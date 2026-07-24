# BIU Grade Watcher

Cross-platform BIU In-Bar grade monitoring for Windows and Linux.

## Supported Platforms

- Windows 10 and Windows 11
- Linux desktop distributions with Python 3.8 or newer

## Platform Detection

The same Python file runs on both systems.

The watcher detects the operating system automatically and selects:

| Feature | Windows | Linux |
|---|---|---|
| Grade monitoring | Playwright | Playwright |
| Direct login | Headless Chromium | Headless Chromium |
| Notifications | Native MessageBox | `notify-send` |
| Snapshot storage | Local JSON | Local JSON |
| Application directory | `%LOCALAPPDATA%\BIUGradeWatcher` | `$XDG_DATA_HOME/biu-grade-watcher` or `~/.local/share/biu-grade-watcher` |

If `notify-send` is not available on Linux, notifications are printed to the terminal.

## Installation

### Windows

```powershell
py -3.8 -m pip install playwright
py -3.8 -m playwright install chromium
```

### Linux

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

Install Linux browser dependencies when required:

```bash
python3 -m playwright install-deps chromium
```

For desktop notifications on Debian or Ubuntu:

```bash
sudo apt install libnotify-bin
```

## Environment File

Create `.env` next to the script:

```env
BIU_ID=123456789
BIU_PHONE=0501234567
```

Alternative variable names are also supported:

```env
id=123456789
phonenumber=0501234567
```

## Windows Usage

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method direct `
    --interval 10 `
    --keepalive 2
```

## Linux Usage

```bash
python3 biu_grade_watcher.py \
    --auth-method direct \
    --interval 10 \
    --keepalive 2
```

## One-Time Test

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

## Manual My Bar-Ilan Login

### Windows

```powershell
py -3.8 biu_grade_watcher.py `
    --auth-method my-biu `
    --force-login
```

### Linux

```bash
python3 biu_grade_watcher.py \
    --auth-method my-biu \
    --force-login
```

A visible Chromium browser is required for the manual portal login.

## Stored Data

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

The directory contains:

```text
browser_profile/
grade_snapshot.json
watcher.log
```

## Reset the Grade Snapshot

### Windows

```powershell
py -3.8 biu_grade_watcher.py --reset
```

### Linux

```bash
python3 biu_grade_watcher.py --reset
```

## Linux Notification Test

```bash
notify-send "BIU Grade Watcher" "Notification test"
```

If this command is unavailable:

```bash
sudo apt install libnotify-bin
```

## Security

Do not commit or upload:

```text
.env
browser_profile/
grade_snapshot.json
watcher.log
```

Recommended `.gitignore`:

```gitignore
.env
browser_profile/
grade_snapshot.json
watcher.log
__pycache__/
*.pyc
```

The OTP is entered interactively and is not saved.
