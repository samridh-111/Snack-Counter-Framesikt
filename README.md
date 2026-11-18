# Snack Counter Automation

A chat-based snack refill automation system for two offices (Oslo & Bengaluru) that solves the classic "who buys the snacks?" problem.

## Problem

- **Bystander effect** — Empty boxes sit for days
- **Duplicate purchases** — Two people buy at the same time
- **Unfair distribution** — Same volunteers always refill
- **No spend visibility** — HR has no real-time budget tracking

## Solution

A state-machine-driven service that:
1. **Detects** empty box reports (dedupes duplicates)
2. **Selects** the fairest volunteer (fewest recent refills → longest since last → deterministic ID)
3. **Reserves** budget on commit (with 20% tolerance)
4. **Creates** idempotent HR claims (auto-retries on outage)
5. **Reports** cross-office spend in NOK with FX conversion

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────┐
│   Chat      │────▶│ RefillService│────▶│   Budget   │
│  (Slack/    │     │  (state      │     │  Ledger    │
│   Teams)    │     │   machine)   │     │ (reserve/  │
└─────────────┘     └──────┬───────┘     │  commit)   │
                           │             └────────────┘
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌───────────┐ ┌──────────┐
         │Selector │ │  FakeHR   │ │  Report  │
         │(fairness)│ │(idempotent)│ │(FX-aware)│
         └─────────┘ └───────────┘ └──────────┘
```

## Quick Start

```bash
git clone https://github.com/samridh-111/Snack-Counter-Framesikt.git
cd Snack-Counter-Framesikt

# Run all tests (58 tests)
python -m pytest tests/ -v

# Run interactive demos
python cli.py all
```

## Demos

| Command | Description |
|---------|-------------|
| `python cli.py 1` | Basic flow: report → accept → receipt → HR approve → reimburse |
| `python cli.py 2` | Offer timeout (10 min) → reassign to next volunteer |
| `python cli.py 3` | Commit deadline (4 hr) missed → release budget → reassign |
| `python cli.py 4` | HR outage → receipt stays pending → auto-retry on recovery |
| `python cli.py 5` | Fairness selector: fewest 30d refills → longest since last |
| `python cli.py 6` | Visitor eligibility: Oslo employee in Bengaluru claims in INR |
| `python cli.py 7` | Monthly report: per-office local currency + NOK grand total |

## Core Components

| File | Purpose |
|------|---------|
| `models.py` | Domain models: Office, Money, Volunteer, Refill, RefillState, AuditEntry |
| `budget.py` | BudgetLedger: per-office monthly ledgers, reservations, FX rates |
| `selector.py` | VolunteerSelector: fairness ranking with blocking rules |
| `fakes.py` | FakeClock, FakeChat, FakeHR for deterministic testing |
| `refill_service.py` | Core lifecycle: state machine, timeouts, escalation, HR integration |
| `report.py` | MonthlyReport generator with cross-office FX totals |
| `cli.py` | Demo CLI showcasing all key flows |

## State Machine

```
EMPTY_REPORTED → OFFERED → COMMITTED → RECEIPT_SUBMITTED → CLAIM_SUBMITTED → REIMBURSED
                    ↓           ↓              ↓
                   DECLINED   RELEASED       RESUBMIT → REJECTED
                    ↓           ↓              ↓
                   OFFERED    OFFERED       ESCALATED (terminal)
```

**Timeouts:**
- Offer: 10 minutes → next volunteer
- Commit: 4 hours → release budget → next volunteer
- Max 3 offer attempts → escalate to admin
- Max 2 resubmits → escalate to admin
- HR claim >10 days → admin alert

## Budget & FX

- **Oslo:** 6,000 NOK/month, estimate 1,200 NOK/refill
- **Bengaluru:** 15,000 INR/month, estimate 3,000 INR/refill
- Reservation on commit, replaced with actual on receipt
- 20% tolerance over estimate before manager approval
- Monthly rollover per office timezone
- FX: injectable dated rate table (INR→NOK for reporting)

## HR Integration

- Idempotent: `refill_id` = idempotency key
- On HR outage: stays in `RECEIPT_SUBMITTED`, background retry
- On rejection: `RESUBMIT` state, volunteer re-uploads receipt
- Max 2 resubmits → `ESCALATED`
- Pending >10 days → admin alert

## Fairness Algorithm

Volunteers ranked by:
1. Fewest refills in last 30 days
2. Longest since last refill
3. Deterministic `employee_id` tiebreak

**Blocked if:**
- Not in office today (`status != OFFICE_TODAY`)
- Not opted in to that office
- Wrong office (unless visitor)
- >2 pending HR claims
- >2,000 NOK / 5,000 INR outstanding

## Tests

```bash
python -m pytest tests/ -v
# 58 tests covering:
# - Concurrency: atomic commit, duplicate dedup
# - Lifecycle: offer timeout, commit deadline, escalation
# - Selector: fairness ranking, blocking, visitor
# - Budget: reserve/release/commit, rollover, FX
# - Report: per-office totals, NOK grand total, avg times
# - HR: idempotency, outage retry, rejection, 10-day alert
# - State transitions: all invalid + happy path
```

## Design Decisions (from `design.md`)

1. **Chat-based** — No IoT sensors; uses existing company chat
2. **Atomic compare-and-set** — Per-refill locks (maps to DB `UPDATE ... WHERE version=?`)
3. **Fairness selector** — Multi-criteria with blocking rules
4. **Budget reservation + tolerance** — Reserve on commit, replace on receipt, 10% tolerance
5. **Idempotent HR claims** — Retry queue with `refill_id` as key
6. **Per-office local currency** — HR handles payroll conversion; injectable FX for reporting
7. **Offer → Commit → Deadline → Reassign** — 10 min / 4 hr / 3 attempts
8. **Minimal receipt validation** — Image URL + amount > 0 + currency match (no OCR)

## Deliberately Not Built

- IoT weight sensors
- OCR receipt scanning
- Live FX API calls
- Multi-currency wallets
- Inventory tracking
- Mobile app / admin dashboard
- Partial refills

## License

MIT