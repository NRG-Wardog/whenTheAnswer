# BIU Grade Watcher v9

Cross-platform BIU In-Bar grade monitoring for Windows and Linux.

## Important design principle

This version reduces the risk of accidental rate limiting or blocking. It does
**not** try to make Playwright undetectable, spoof a browser fingerprint, solve
CAPTCHAs, or bypass BIU security controls.

When BIU returns a blocking response or human-verification challenge, the
watcher pauses automatically and does not keep retrying.

## Protection mechanisms

- Persistent Chromium profile and session.
- One running watcher instance per user.
- No parallel BIU operations.
- Minimum spacing between top-level requests.
- Hourly request budget.
- Random jitter around check and keepalive times.
- Detection of HTTP `403`, `406`, `418`, `423`, and `429`.
- Detection of blocking, CAPTCHA, and rate-limit page text.
- `Retry-After` support.
- Persistent `CLOSED`, `OPEN`, and `HALF_OPEN` circuit breaker.
- Exactly one controlled recovery probe after cooldown.
- Exponential backoff for transient server/network failures.
- Authentication attempt budget.
- No repeated automatic OTP attempts.
- Keepalive uses `fetch()` inside the authenticated browser context.
- No stealth or fingerprint-spoofing browser flags.

## Installation

### Windows

```powershell
py -3.8 -m pip install -r requirements.txt
py -3.8 -m playwright install chromium
```

### Linux

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 -m playwright install-deps chromium
```

Desktop notifications on Debian/Ubuntu:

```bash
sudo apt install libnotify-bin
```

## Environment file

Create `.env` next to the script:

```env
BIU_ID=123456789
BIU_PHONE=0501234567
```

Alternative names remain supported:

```env
id=123456789
phonenumber=0501234567
```

The OTP is entered interactively and is not stored.

## Recommended command

### Windows

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method direct `
    --interval 10 `
    --keepalive 2
```

### Linux

```bash
python3 biu_grade_watcher.py \
    --auth-method direct \
    --interval 10 \
    --keepalive 2
```

The actual schedule includes small random jitter to avoid synchronized request
bursts.

## One-time test

### Windows

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method direct `
    --force-login `
    --once `
    --print-rows
```

### Linux

```bash
python3 biu_grade_watcher.py \
    --auth-method direct \
    --force-login \
    --once \
    --print-rows
```

## Protection status

```powershell
py -3.8 biu_grade_watcher.py --protection-status
```

or:

```bash
python3 biu_grade_watcher.py --protection-status
```

Example:

```json
{
  "circuit_state": "OPEN",
  "consecutive_failures": 0,
  "opened_count": 1,
  "cooldown_until": "2026-07-24T18:00:00+00:00",
  "last_reason": "grade check returned HTTP 429.",
  "last_status": 429,
  "seconds_until_probe": 1620
}
```

## Clearing the protection state

Only clear it after confirming that BIU is accessible normally in a regular
browser:

```powershell
py -3.8 biu_grade_watcher.py --clear-protection-state
```

The state is intentionally persistent. Restarting the script does not erase a
cooldown after a `403`, `429`, CAPTCHA, or blocking page.

## Stored data

### Windows

```text
%LOCALAPPDATA%\BIUGradeWatcher
```

### Linux

```text
$XDG_DATA_HOME/biu-grade-watcher
```

or:

```text
~/.local/share/biu-grade-watcher
```

Files:

```text
browser_profile/
grade_snapshot.json
protection_state.json
watcher.log
watcher.lock
```

## Safety limits

The defaults are deliberately conservative:

```text
Grade check interval: 10 minutes
Keepalive interval: 2 minutes
Minimum top-level request gap: 12 seconds
Maximum top-level operations: 45 per hour
```

The script refuses a grade interval below 5 minutes, a keepalive interval below
2 minutes, or more than 60 top-level operations per hour.

## What happens after a block

1. The watcher records the reason and status.
2. The circuit changes to `OPEN`.
3. Keepalive and grade checks pause.
4. The cooldown survives process restarts.
5. After the cooldown, one `HALF_OPEN` recovery probe is allowed.
6. Success closes the circuit.
7. Failure reopens it with a longer cooldown.

The watcher does not attempt to bypass the restriction.
