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
| Safe-disconnect | `deconnexion_safe.bat` (`tscon`) | must keep working after scalper install |

The scalper runs as a **second, completely independent** process with its own MT5 terminal and demo account. The two bots share the VPS but must never share a terminal or account.

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

### 1.4 Install Python dependencies
```powershell
cd C:\Users\BotVm\Desktop\xauusd-scalper
pip install -r requirements.txt
```

### 1.5 Verify MT5 connection before scheduling
```powershell
python -c "
import mt5_client
assert mt5_client.is_connected(), 'NOT connected'
import MetaTrader5 as mt5
info = mt5.account_info()
print('Login:', info.login, '  Server:', info.server)
mt5.shutdown()
"
```
Expected output: `Login: 12345678  Server: Exness-MT5Trial`  
If it prints the **signals bot's** login number, `MT5_TERMINAL_PATH` is wrong — fix `.env`.

---

## 2. Install Task Scheduler task (scalper main process)

> Must run as Administrator in an RDP session on botvm.

```powershell
cd C:\Users\BotVm\Desktop\xauusd-scalper
.\scripts\install_task.ps1 -User BotVm -Force
```

Verify:
```powershell
Get-ScheduledTask -TaskName "xauusd-scalper" | Select-Object TaskName, State
```

The task starts automatically at next BotVm logon and restarts on failure (1 min interval, 3 attempts).

> **Why interactive session?** MT5's Python API communicates over a named pipe that only exists in the session where `terminal64.exe` is running. Task Scheduler with `LogonType Interactive` ensures the bot runs in the same session. Running as session-0 (SYSTEM / NSSM) would silently fail to connect.

---

## 3. Install dashboard service (optional)

```powershell
.\scripts\install_dashboard_task.ps1   # defaults assume paths above
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

### Daily watch (10 seconds)
```powershell
.\scripts\watch.ps1
```
Shows: heartbeat age, open positions, today's PnL, last 3 log lines.  
Prints restart instructions if heartbeat is > 15 min stale.

### Update bot (after a git push from dev)
```powershell
# Safe (refuses if position open):
.\scripts\update_bot.ps1

# Override (use only when position is confirmed safe to interrupt):
.\scripts\update_bot.ps1 -Force
```

### Regenerate Excel journal manually
```powershell
python -m reporting.excel_export
# Output: data\scalper_journal.xlsx
```
Also runs automatically from `main.py` at 22:30 UTC daily.

### Restart scalper manually
```powershell
schtasks /End /TN xauusd-scalper
schtasks /Run /TN xauusd-scalper
```

### View live logs
```powershell
Get-Content logs\scalper-wrapper.log -Wait -Tail 20
```

---

## 6. deconnexion_safe.bat compatibility

The existing `deconnexion_safe.bat` uses `tscon` to transfer the RDP session to the console — this keeps interactive processes alive (Task Scheduler interactive tasks, MT5 terminals).

After installing the scalper task:
1. Run `deconnexion_safe.bat` as usual.
2. Re-RDP into botvm.
3. Verify both `xauusd-detector` (Task Scheduler) and `xauusd-scalper` (Task Scheduler) are still running:

```powershell
Get-ScheduledTask -TaskName "xauusd-detector","xauusd-scalper" | Select-Object TaskName, State
```

Both should show `Running`. If either shows `Ready` (stopped), it was not in the interactive session — check the `LogonType` setting.

---

## 7. Go-live (demo) checklist

- [ ] `.env` complete, `MT5_TERMINAL_PATH=C:\MT5_scalper\terminal64.exe`
- [ ] `account_info().login` check passes (scalper demo account, NOT signals account)
- [ ] Task `xauusd-scalper` installed, state = Running
- [ ] Survives `deconnexion_safe.bat` disconnect (both tasks still Running after re-RDP)
- [ ] Inject-test-signal full lifecycle verified on the VPS:
      `python main.py --inject-test-signal`  → signal in DB → order placed → managed → closed
- [ ] Crash-recovery verified (kill python PID with position open → task restarts → reconcile_pending_and_orphans picks up position)
- [ ] Dashboard reachable at `http://127.0.0.1:8080` via RDP browser
- [ ] First daily Excel export generated (`data\scalper_journal.xlsx` exists)
- [ ] Heartbeat row present in DB (watch.ps1 shows `OK`)
- [ ] Resource check completed (see §4), scan interval decided

**Start date recorded:** ___________  
**Demo measurement period:** 3–6 months  
**Minimum trades before judging winrate:** 100 closed trades per strategy  

---

## 8. Troubleshooting quick-reference

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `MT5 not connected — skipping tick` | Wrong `MT5_TERMINAL_PATH` or terminal not running | Check `.env`, ensure `C:\MT5_scalper\terminal64.exe` is open and logged in |
| Task shows `Ready` after logoff | Task registered with wrong logon type | Re-run `install_task.ps1 -Force` |
| Dashboard 500 / import error | Missing dependency | `pip install -r requirements.txt` |
| Two positions open simultaneously | Magic number collision with signals bot | Verify scalper uses 20001–20004 and signals bot uses different magic numbers |
| Heartbeat stale > 15 min | Bot crashed or hung | `watch.ps1` will print restart command; check `logs\scalper.log` for traceback |
| `account_info().login` returns signals account | MT5_TERMINAL_PATH still points to default terminal | Set `MT5_TERMINAL_PATH=C:\MT5_scalper\terminal64.exe` in `.env` |
