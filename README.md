# whenTheAnswer

## Authentication Modes

### Direct login

Direct login runs fully headless.

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method direct `
    --force-login
```

The watcher:

1. opens Chromium in headless mode;
2. fills the BIU ID and phone fields;
3. submits the first login step;
4. asks for the OTP in PowerShell;
5. submits the OTP in the same headless browser context;
6. monitors grades without opening a browser window.

### My Bar-Ilan

Manual portal login uses a visible browser.

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method my-biu `
    --force-login
```

After authentication, the browser is minimized and kept alive.

## Environment File

```env
BIU_ID=123456789
BIU_PHONE=0501234567
```

Alternative names are also supported:

```env
id=123456789
phonenumber=0501234567
```

Never store the OTP in `.env`.

## Session Keepalive

BIU may expire inactive sessions quickly.

The watcher therefore runs a lightweight authenticated keepalive independently
from the grade-check schedule.

Default configuration:

```text
Grade check: every 10 minutes
Keepalive: every 2 minutes
```

Run:

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method direct `
    --interval 10 `
    --keepalive 2
```

Example output:

```text
[2026-07-24 15:00:00] Browser mode: headless.
[2026-07-24 15:00:00] Checking BIU In-Bar for new grades now.
[2026-07-24 15:00:04] Check completed: 51 rows, no new grades.
[2026-07-24 15:00:04] Waiting 10 minute(s) before the next grade check.
[2026-07-24 15:02:04] BIU session keepalive completed successfully.
[2026-07-24 15:04:04] BIU session keepalive completed successfully.
```

## Recommended Command

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method direct `
    --interval 10 `
    --keepalive 2
```

## One-Time Test

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method direct `
    --force-login `
    --once `
    --print-rows
```
