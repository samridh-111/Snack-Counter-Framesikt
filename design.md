# Snack Counter Automation — Design

## Refill Lifecycle

```
EMPTY_REPORTED → OFFERED → COMMITTED → RECEIPT_SUBMITTED → CLAIM_SUBMITTED → REIMBURSED
                    ↓           ↓              ↓                   ↓
                  DECLINED   RELEASED       (HR down)           REJECTED
                    ↓           ↓              ↓                   ↓
                  OFFERED*   OFFERED*      CLAIM_SUBMITTED*    RESUBMIT → RECEIPT_SUBMITTED
                    ↓                                                    ↓
                 ESCALATED                                            REJECTED (max 2) → ESCALATED
```

**Happy path**: Volunteer reports empty → system offers to best candidate → candidate accepts (COMMITTED, budget reserved) → buys snacks, uploads receipt → system creates HR claim (idempotent) → HR approves → REIMBURSED.

**Unhappy paths**: Duplicate empty report → merged into existing OPEN refill. Offer declined/timeout → next candidate. Commit deadline missed → budget released, volunteer notified, reassigned. HR unavailable → claim stays in outbox, retried. HR rejects → volunteer resubmits (max 2×). Claim >10 days pending → admin alert. After N failures → ESCALATED to office admin.

---

## Components & Data

| Component | Responsibility | Data Kept |
|-----------|----------------|-----------|
| **RefillStore** | Refill lifecycle, state transitions, audit log | `Refill{id, office, state, version, volunteer_id, offered_at, committed_at, receipt_amount, receipt_image, claim_id, resubmit_count, audit_log[]}` |
| **VolunteerDirectory** | Volunteer info, availability, office presence | `Volunteer{id, name, home_office, current_office, status, opted_in_offices, last_refill_at, refill_count_30d, pending_claims}` |
| **BudgetLedger** | Per-office monthly budget, reservations, FX rates | `MonthlyBudget{office, year, month, limit, spent, reserved, currency}`, `ExchangeRate{from_currency, to_currency, rate, valid_from}` |
| **ChatAdapter** | Send DM/notifications | `send_dm(user_id, text)`, `send_channel(channel, text)` |
| **HRAdapter + Outbox** | Submit claims, handle retries, idempotency | `create_claim(refill_id, employee_id, amount, currency, description, receipt_url) → claim_id`, outbox queue for retries |
| **ReportGenerator** | Monthly cross-office report | Aggregates from RefillStore + BudgetLedger + FX rates |

---

## Problem → Mechanism

| Problem | Mechanism |
|---------|-----------|
| Box stays empty days | Auto-offer to best volunteer with 10-min accept timeout; escalation after 3 failures |
| Duplicate purchases | Single OPEN refill per office; atomic compare-and-set commit (only one wins) |
| Unfair load / long wait for money | Selector ranks by fewest refills (30d) → longest since last → deterministic ID tiebreak; blocks volunteers with >2 pending claims |
| No spend visibility | Monthly report: per-office local totals, NOK grand total via injected FX, budget %, refill count, avg empty→refilled, avg purchase→reimbursed |

---

## Trade-offs

- **No OCR**: Receipt validation is minimal (image URL present, amount>0, currency matches). Manual HR review handles correctness.
- **No live FX**: Management report uses injected dated rate table (e.g., month-end rate). HR pays in employee's payroll currency.
- **No IoT sensors**: Relies on human "empty" report via chat command.
- **In-memory with locks**: Suitable for ~35 volunteers; production would use DB with conditional UPDATE.
- **Single-threaded service instance**: Simplifies locking; scale via sharding by office if needed.

---

## Deliberately Not Built

| Feature | Reason |
|---------|--------|
| OCR receipt parsing | Unreliable; HR already reviews |
| IoT weight sensors | Cost/complexity unjustified for 2 offices |
| Live FX API | Rate table injected monthly; HR handles payroll conversion |
| Web/mobile UI | Chat bot is the interface (existing tool) |
| Auth/permissions | Company login + chat identity sufficient |
| Multi-currency wallets | HR pays in payroll currency; we only submit in purchase currency |
| Inventory tracking | Out of scope; only refill automation needed |

---

## Assumptions

1. Volunteers report "empty" via `/snack empty` in chat; bot DMs offers.
2. Office timezones: Oslo = CET/CEST, Bengaluru = IST (no DST).
3. Monthly budget resets at 00:00 local time on 1st of month.
4. HR approves/rejects within 1–10 days; we poll or receive webhook.
5. Max 2 resubmits before escalation.
6. Offer timeout = 10 min; commit deadline = 4 hours.
7. Volunteer "present" = status `OFFICE_TODAY` in that office (visitors count if opted in).
8. Budget currency = office local currency (NOK/INR); never mixed.
9. Refill ID = UUID; used as HR idempotency key.
10. Single service instance per office (or shared with office-scoped locks).
