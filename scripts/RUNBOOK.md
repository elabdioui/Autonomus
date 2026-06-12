# xauusd-scalper — VPS Deployment Runbook (botvm)

> Demo-only. No live capital. Target: 3–6 months, ≥ 100 closed trades per strategy before judging winrate.

---

## 0. Existing infrastructure — do NOT disturb

| Component | Path / Name | Notes |
|-----------|-------------|-------|
| Signals detector | Task Scheduler → `xauusd-detector` | interactive session, `run_detector.bat` |
| Backend API | NSSM service `xauusd-backend` | session 0, port 8000 |
| MT5 terminal (signals) | default install `C:\Program Files\...` | logged into signals demo account |
| NSSM binary | `C:\tools\nssm-2.24-101-g897c7ad\win64\nssm.exe` | reused by dashboard service |
| Safe-disconnect | `.\ops\bot.ps1 disconnect` (`tscon`) | must keep working after scalper install |

The scalper runs as a **second, completely independent** process with its own MT5 terminal and demo account. The two bots share the VPS but must never share a terminal or account.

### Isolation contract

| Concern | Signals bot — DO NOT TOUCH | Scalper (this repo) |
|---------|---------------------------|---------------------|
| Repo path | `C:\Users\BotVm\Desktop\xauusd\` | `C:\Users\BotVm\Desktop\xauusd-scalper\` |
| Python process match | cmdline contains `detector` | cmdline contains `xauusd-scalper` AND `main.py` |
| MT5 terminal | default install path | `C:\MT5_scalper\terminal64.exe` only |
| Task Scheduler | `xauusd-detector` | `xauusd-scalper` |
| NSSM services | `xauusd-backend` (port 8000) | `xauusd-scalper-dashboard` (port 8080, optional) |
| Python env | repo `.venv` | repo `.venv` |
| DB / logs | inside its repo | `data\`, `logs\` inside this repo |

---

## 1. Manual prerequisites (one-time, do in order)

### 1.1 Create new Exness demo account
1. Log into Exness client portal → open a new **MT5 demo** account.
2. Note: `login` (e.g. `12345678`), `server` (e.g. `Exness-MT5Trial`).
3. Write credentials into `.env` before proceeding.

### 1.2 Install MT5 portable
```
# On botvm, open a browser and download the MT5 installer from Exness.
# Choose "portable" / custom path when prompted, or copy an existing
# MT5 folder and re-login.

1. Install / copy to: C:\MT5_scalper\
2. Launch: C:\MT5_scalper\terminal64.exe /portable
3. Log in to the NEW demo account (login + server from step 1.1).
4. Tools → Options → Expert Advisors → ✓ "Allow algorithmic trading"
5. Confirm XAUUSDm is visible in Market Watch (add if missing).
6. Leave terminal running — do NOT close it.
```

> **Why portable?** Keeps scalper terminal 100 % isolated from the signals terminal. `/portable` flag stores all settings inside `C:\MT5_scalper\` instead of `%APPDATA%\MetaQuotes\`.

### 1.3 Clone repo and configure
```powershell
cd C:\Users\BotVm\Desktop
git clone https://github.com/elabdioui/xauusd-scalper.git
cd xauusd-scalper

# Create .env from template
copy .env.example .env
notepad .env   # fill MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, FINNHUB_API_KEY
```

Minimum required `.env` values:
```
MT5_LOGIN=<new demo login>
MT5_PASSWORD=<password>
MT5_SERVER=<server name>
MT5_TERMINAL_PATH=C:\MT5_scalper\terminal64.exe
SYMBOL=XAUUSDm
LOT=0.2
FINNHUB_API_KEY=<reuse existing key>
```

---

## 2. Bootstrap (one command — as Administrator)

> Must run as Administrator in an RDP session on botvm.

```powershell
cd C:\Users\BotVm\Desktop\xauusd-scalper
.\ops\install.ps1
```

`install.ps1` does everything in one shot:
- Verifies Python ≥ 3.11, git, `.env`
- Creates `.venv` and runs `pip install -r requirements.txt`
- Validates `MT5_TERMINAL_PATH` points to `C:\MT5_scalper\`
- Confirms MT5 account identity interactively (abort on wrong account)
- Registers Task Scheduler task `xauusd-scalper` (AtLogOn BotVm, interactive, RunLevel Highest, restart 1 min ×3)
- Installs NSSM dashboard service if NSSM present (optional)
- Runs `.\ops\bot.ps1 status`

Safe to re-run: all steps print `[OK]` or `[SKIP]`, no destructive action on repeat.

Verify afterwards:
```powershell
.\ops\bot.ps1 status
```

The task starts automatically at next BotVm logon and restarts on failure (1 min interval, 3 attempts).

> **Why interactive session?** MT5's Python API communicates over a named pipe that only exists in the session where `terminal64.exe` is running. Task Scheduler with `LogonType Interactive` ensures the bot runs in the same session. Running as session-0 (SYSTEM / NSSM) would silently fail to connect.

---

## 3. Dashboard service (optional)

Installed automatically by `install.ps1` if NSSM is found at:
```
C:\tools\nssm-2.24-101-g897c7ad\win64\nssm.exe
```

Access via RDP browser: `http://127.0.0.1:8080`  
Or SSH tunnel from dev machine: `ssh -L 8080:127.0.0.1:8080 botvm`

---

## 4. Resource check — both bots running simultaneously

During one full **NY_AM killzone** (14:30–17:00 UTC) with both bots scanning:

| Metric | How to measure | Alert threshold |
|--------|---------------|-----------------|
| RAM used | Task Manager → Performance | > 80% → raise `SCAN_INTERVAL_SECONDS=10` |
| CPU peak | Task Manager → 1-min avg | > 85% sustained → raise interval |
| MT5 (signals) memory | Task Manager → Details | watch for leaks over time |
| MT5 (scalper) memory | same | |

**If RAM > 80% or CPU > 85% sustained** → edit `.env`: `SCAN_INTERVAL_SECONDS=10`, restart scalper. If still insufficient → consider VPS upgrade before extending demo period.

Record baseline here when measured:

```
Date measured   : ___________
RAM total / used: ___ GB / ___ GB  (___ %)
CPU peak        : ___ %
MT5 signals mem : ___ MB
MT5 scalper mem : ___ MB
Decision        : 5 s interval / 10 s interval / upgrade
```

---

## 5. Routine operations

### Daily status (10 seconds)
```powershell
.\ops\bot.ps1 status
```
Shows: task state, python process, heartbeat age + killzone, open positions, today's PnL, all-time PnL, git HEAD.

### Update bot (after a git push from dev)
```powershell
# Safe (refuses if position open):
.\ops\bot.ps1 update

# Override (use only when position is confirmed safe to interrupt):
.\ops\bot.ps1 update -Force
```

### Restart scalper
```powershell
.\ops\bot.ps1 restart
```

### View live logs
```powershell
.\ops\bot.ps1 logs -Wait
```

### Key event log grep
```powershell
.\ops\bot.ps1 logs stats
```

### Regenerate Excel journal manually
```powershell
.venv\Scripts\python.exe -m reporting.excel_export
# Output: data\scalper_journal.xlsx
```
Also runs automatically from `main.py` at 22:30 UTC daily.

---

## 6. deconnexion_safe.bat compatibility

```powershell
.\ops\bot.ps1 disconnect
```

Uses `tscon` to transfer the RDP session to the console — keeps interactive processes alive (Task Scheduler interactive tasks, MT5 terminals). One disconnect covers both bots (same Windows session).

After reconnecting, verify both tasks still running:
```powershell
Get-ScheduledTask -TaskName "xauusd-detector","xauusd-scalper" | Select-Object TaskName, State
```

Both should show `Running`. If either shows `Ready` (stopped), the task was not in the interactive session — re-run `install.ps1`.

---

## 7. Go-live (demo) checklist

- [ ] `.env` complete, `MT5_TERMINAL_PATH=C:\MT5_scalper\terminal64.exe`
- [ ] `install.ps1` ran as Administrator, account identity confirmed
- [ ] `.\ops\bot.ps1 status` shows `FINAL: OK`
- [ ] Survives `.\ops\bot.ps1 disconnect` (both tasks still Running after re-RDP)
- [ ] Inject-test-signal full lifecycle verified on the VPS:
      `.venv\Scripts\python.exe main.py --inject-test-signal`  → signal in DB → order placed → managed → closed
- [ ] Crash-recovery verified (kill python PID with position open → task restarts → reconcile_pending_and_orphans picks up position)
- [ ] Dashboard reachable at `http://127.0.0.1:8080` via RDP browser
- [ ] First daily Excel export generated (`data\scalper_journal.xlsx` exists)
- [ ] Heartbeat row present in DB (`bot.ps1 status` shows `OK`)
- [ ] Resource check completed (see §4), scan interval decided

**Start date recorded:** ___________  
**Demo measurement period:** 3–6 months  
**Minimum trades before judging winrate:** 100 closed trades per strategy  

---

## 8. Troubleshooting quick-reference

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `MT5 not connected — skipping tick` | Wrong `MT5_TERMINAL_PATH` or terminal not running | Check `.env`, ensure `C:\MT5_scalper\terminal64.exe` is open and logged in |
| Task shows `Ready` after logoff | Task registered with wrong logon type | Re-run `.\ops\install.ps1` |
| Dashboard 500 / import error | Missing dependency | `.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| Two positions open simultaneously | Magic number collision with signals bot | Verify scalper uses 20001–20004 and signals bot uses different magic numbers |
| Heartbeat stale > 15 min | Bot crashed or hung | `.\ops\bot.ps1 status` shows details; `.\ops\bot.ps1 restart` |
| `account_info().login` returns signals account | MT5_TERMINAL_PATH still points to default terminal | Set `MT5_TERMINAL_PATH=C:\MT5_scalper\terminal64.exe` in `.env` |
