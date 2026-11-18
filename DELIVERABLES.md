# Snack Counter Automation — Complete Deliverables

## 1. Key Decisions (with Justifications)

1. **Chat-based interaction via slash commands** — No IoT sensors; leverages existing company chat tool that all employees already use on mobile/desktop
2. **Atomic compare-and-set state machine** — Prevents duplicate purchases using per-refill locks (maps to DB conditional UPDATE in production)
3. **Fairness selector with multiple criteria** — Fewest refills in 30 days → longest since last refill → employee_id tiebreak; blocks volunteers with >2 pending claims or >2000 NOK/5000 INR outstanding
4. **Budget reservation + tolerance model** — Reserve max typical purchase on commit; replace with actual on receipt; allow 10% overspend tolerance; flag for manager approval beyond that
5. **Idempotent HR claims with retry queue** — Use refill_id as idempotency key; on HR outage, stay in RECEIPT_SUBMITTED with claim_pending=true; background retry recovers
6. **Per-office local currency with NOK reporting** — Oslo=NOK, Bengaluru=INR; HR handles payroll conversion; injectable dated FX table for management report
7. **Offer → Commit → Deadline → Reassign flow** — 10 min offer timeout, 4 hour commit deadline, 3 failed attempts escalate to admin
8. **Minimal receipt validation** — Image URL present, amount > 0, currency matches office; no OCR (unreliable, out of scope)

---

## 2. Design Document
See `design.md` for full design document covering:
- State machine with happy/unhappy paths
- Component architecture and data stored
- Problem → mechanism mapping
- Trade-offs and non-goals
- Explicit assumptions

---

## 3. Source Code Files

All source files are in the project root:
- `models.py` — Domain models (Office, Money, Volunteer, Refill, RefillState, HRClaim, AuditEntry)
- `budget.py` — BudgetManager with reservations, monthly rollover, FX conversion
- `selector.py` — VolunteerSelector with fairness ranking
- `fakes.py` — FakeChat, FakeHR, FakeClock for testing
- `refill_service.py` — Core lifecycle service with state machine
- `report.py` — MonthlyReport generator with cross-office FX totals

---

## 4. Test Files

All tests in `tests/` directory:
- `test_concurrency.py` — Atomic commits, duplicate deduplication
- `test_lifecycle.py` — Offer timeout, commit deadline, escalation
- `test_selector.py` — Fairness ranking, blocking rules, visitor eligibility
- `test_budget.py` — Reservations, actual replacement, monthly rollover, FX
- `test_report.py` — Monthly report with NOK grand total
- `test_hr_reimbursement.py` — Idempotency, HR outage retry, rejection flow, 10-day alert
- `test_state_transitions.py` — Invalid transitions, valid happy path

---

## 5. Run Tests

```bash
cd /Users/samridhsuresh/snack-counter
python -m pytest tests/ -v
```

**Expected Output:** 57 tests passed, 0 failed

---

## 6. Walkthrough Script (10-minute talking outline)

### Minute 0-1: Problem Statement
"Two offices (Oslo 15 volunteers, Bengaluru 20), each with snack counter. Problems: box sits empty days, duplicate purchases, unfair load, no visibility. HR system external — we submit claims with amount, currency, receipt, employee ID."

### Minute 1-2: Architecture Overview
"Five core components: RefillService (state machine), Selector (fairness), BudgetManager (reservations + FX), FakeChat/FakeHR (stand-ins for external systems). All dependencies injected — testable, swappable."

### Minute 2-3: State Machine
"Refill moves: EMPTY → OPEN → OFFERED → COMMITTED → RECEIPT_SUBMITTED → REIMBURSED. Unhappy paths: duplicate empty→dedup, offer timeout→next volunteer, commit deadline→release+reoffer, HR down→retry queue, HR reject→resubmit (max 2). Every transition audited."

### Minute 3-4: Atomic Commit & Deduplication
"Concurrent commits prevented by per-refill lock (threading.RLock, maps to DB `UPDATE ... WHERE state='OPEN' AND version=X`). Duplicate 'empty' reports deduped to single OPEN refill — test proves exactly one refill created."

### Minute 4-5: Fairness Selector
"Ranks eligible volunteers (present today, opted in, not blocked): 1) fewest refills last 30 days, 2) longest since last refill, 3) employee_id. Blocks: >2 pending claims OR >2000 NOK/5000 INR outstanding. Visitor from Oslo in Bengaluru? Eligible for Bengaluru, claim in INR."

### Minute 5-6: Budget & FX
"Reserve max typical purchase (Oslo 1200 NOK, Bengaluru 3000 INR) on commit. On receipt, replace with actual. Tolerance: 10% over reservation. Monthly rollover per office timezone. FX: injectable dated rate table, convert to NOK for management report."

### Minute 6-7: HR Integration
"Idempotent claim creation (refill_id = idempotency key). HR down? Stay in RECEIPT_SUBMITTED, background retry. HR reject? State → RESUBMIT, volunteer re-uploads. Max 2 resubmits → escalate. >10 days pending → admin alert."

### Minute 7-8: Reporting
"Monthly report: per-office local currency totals, budget %, refill count, avg empty→refilled, avg purchase→reimbursed, optional item breakdown. Grand total in NOK with rate and date used."

### Minute 8-9: Trade-offs
"Did NOT build: IoT sensors, OCR, live FX, multi-currency wallets, inventory tracking, mobile app, admin dashboard. Justified in design doc."

### Minute 9-10: Tests
"57 tests covering all requirements. Key: concurrent commit (threading), offer timeout (fake clock), commit deadline, max attempts escalate, fairness ranking, visitor eligibility, budget tolerance, FX conversion, HR idempotency/outage/rejection, invalid state transitions."

---

## 7. 5 Likely Tough Interview Questions with Short Answers

**Q1: "What happens if two volunteers simultaneously press 'accept' on the same offer?"**
A: Per-refill lock (threading.RLock) ensures atomic compare-and-set. In production, maps to `UPDATE refills SET state='COMMITTED', volunteer_id=?, version=version+1 WHERE id=? AND state='OFFERED' AND volunteer_id=? AND version=?`. Exactly one succeeds; other gets InvalidStateTransitionError.

**Q2: "How do you handle HR system being down for hours?"**
A: Claim creation is wrapped in try/except. On failure, refill stays in RECEIPT_SUBMITTED with claim_id=None. Background job `retry_pending_claims()` polls and retries. Idempotency key (refill_id) prevents duplicate claims on retry. Test proves recovery.

**Q3: "What if a volunteer buys snacks in Bengaluru but their payroll is in NOK?"**
A: Purchase currency = office local currency (INR). HR claim submitted in INR. HR handles payroll conversion to employee's currency. Budget tracked in INR. Management report converts to NOK using injectable FX rate.

**Q4: "How do you prevent the same few people always buying?"**
A: Selector ranks by fewest refills in 30-day window, then longest since last refill. Volunteers with >2 pending claims or >2000 NOK/5000 INR outstanding are blocked until reimbursed. Deterministic tiebreak by employee_id.

**Q5: "What if the receipt amount exceeds the budget reservation?"**
A: 10% tolerance over reservation allowed. Beyond that → flag for manager approval (chat alert to #admin). If exceeds remaining monthly budget → flag for manager approval. Never silently loses money — always alerts.

---

## 8. 5 Questions I Would Ask Interviewers (and Assumptions Made)

| # | Question | Assumption Made |
|---|----------|-----------------|
| 1 | Is a chat bot allowed to DM volunteers directly, or only post to channels? | Assumed DMs allowed for offers; channels for alerts/escalation |
| 2 | Who is the "office admin" for escalation — a specific role or rotating? | Assumed "admin" channel exists; escalation posts there |
| 3 | Should the monthly report cover purchase date or reimbursement date? | Assumed purchase date (receipt_submitted_at) for spend; reimbursement date for aging |
| 4 | Does HR support webhooks for approval/rejection, or only polling? | Assumed polling with optional webhook callback (both supported) |
| 5 | Should volunteers be able to partially refill (add items without emptying)? | Assumed full refill cycle only; partial refill = separate feature |