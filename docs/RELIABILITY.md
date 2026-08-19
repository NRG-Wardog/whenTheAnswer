# Reliability Invariants

`whenTheAnswer` is designed around a small set of explicit invariants.

## External-control invariants

- A CAPTCHA, blocking page, or explicit block status is treated as a stop condition.
- HTTP 429 honors `Retry-After` when supplied.
- The watcher does not use stealth, fingerprint spoofing, or challenge bypass techniques.

## Scheduling invariants

- Only one watcher process may own the local state at a time.
- Top-level operations are serialized.
- A minimum request gap and hourly request budget apply independently of the normal polling interval.
- Periodic work includes jitter to avoid exact fixed scheduling.

## Recovery invariants

- Protection state survives process restarts.
- Repeated transient failures can open the circuit.
- Explicit protection events open the circuit immediately.
- An open circuit prevents normal activity until cooldown expires.
- Recovery is serialized through a controlled `HALF_OPEN` workflow.
- A successful recovery returns the circuit to `CLOSED` and clears transient state.

## Test boundary

The deterministic unit tests exercise parsing, state transitions, persistence, and circuit behavior without contacting BIU or launching a browser. Live browser authentication remains an integration concern and is intentionally excluded from public CI.
