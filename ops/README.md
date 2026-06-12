# xauusd-scalper — Ops Tooling

Single-page reference for the `ops\` entry points. Everything you need for daily operation, fresh VM setup, and the isolation contract that keeps the scalper from touching the signals bot.

---

## Commands

| Command | What it does |
|---------|-------------|
| `.\ops\bot.ps1` | Same as `status` |
| `.\ops\bot.ps1 status` | Scheduled task · python process · wrapper log · MT5 terminal · dashboard · DB heartbeat + stats · git HEAD |
| `.\ops\bot.ps1 start` | Start scheduled task (fallback: bat file); start dashboard if installed; wait 15 s; show status |
| `.\ops\bot.ps1 stop` | Kill scalper python (scoped match); stop dashboard service |
| `.\ops\bot.ps1 restart` | stop + start (dashboard untouched) |
| `.\ops\bot.ps1 restart -All` | stop + start including dashboard service |
| `.\ops\bot.ps1 update` | Guard open positions → git pull → pip if req changed → restart → tail log → status |
| `.\ops\bot.ps1 update -Force` | Same but skips open-position guard |
| `.\ops\bot.ps1 logs` | Last 30 lines of `logs\scalper-wrapper.log` |
| `.\ops\bot.ps1 logs -Wait` | Same with `-Wait` (live tail) |
| `.\ops\bot.ps1 logs stats` | Grep `logs\scalper.log` for key events |
| `.\ops\bot.ps1 disconnect` | `tscon` — transfers RDP session to console, keeping interactive tasks alive |

---

## Fresh VM — setup sequence

```
1. Install MT5 portable
   - Download from Exness broker portal.
   - Install to C:\MT5_scalper\  (use /portable flag).
   - Launch: C:\MT5_scalper\terminal64.exe /portable
   - Log into NEW scalper demo account.
   - Tools → Options → Expert Advisors → ✓ Allow algorithmic trading.
   - Add XAUUSDm to Market Watch.
   - Leave terminal open.

2. Clone repo
   cd C:\Users\BotVm\Desktop
   git clone https://github.com/elabdioui/xauusd-scalper.git
   cd xauusd-scalper

3. Configure .env
   copy .env.example .env
   notepad .env
   # Required: MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, FINNHUB_API_KEY
   # MT5_TERMINAL_PATH=C:\MT5_scalper\terminal64.exe

4. Run install.ps1 (as Administrator)
   .\ops\install.ps1

   install.ps1 will:
     - Verify Python ≥ 3.11, git, .env
     - Create .venv and pip install
     - Validate MT5_TERMINAL_PATH points to C:\MT5_scalper\
     - Confirm MT5 account identity interactively
     - Register Task Scheduler task xauusd-scalper
     - Install NSSM dashboard service if NSSM present (optional)
     - Run bot.ps1 status

5. Verify
   .\ops\bot.ps1 status
```

---

## Daily 10-second routine

```powershell
.\ops\bot.ps1 status
```

Shows: task state · python process · heartbeat age + killzone · open positions · today's PnL · all-time PnL · git HEAD.

---

## Isolation contract

Two bots share one Windows VM (`botvm`). Each is strictly isolated:

| Concern | Signals bot — DO NOT TOUCH | Scalper (this repo) |
|---------|---------------------------|---------------------|
| Repo path | `C:\Users\BotVm\Desktop\xauusd\` | `C:\Users\BotVm\Desktop\xauusd-scalper\` |
| Python process match | cmdline contains `detector` | cmdline contains `xauusd-scalper` AND `main.py` |
| MT5 terminal | default install path | `C:\MT5_scalper\terminal64.exe` only |
| Task Scheduler | `xauusd-detector` | `xauusd-scalper` |
| NSSM services | `xauusd-backend` (port 8000) | `xauusd-scalper-dashboard` (port 8080, optional) |
| Python env | repo `.venv` | repo `.venv` |
| DB / logs | inside its repo | `data\`, `logs\` inside this repo |

Hard rules enforced in `ops\bot.ps1`:

1. Every process kill/start filters on `*xauusd-scalper*main.py*` in CommandLine.
2. MT5 checks filter `Get-Process terminal64` by `$_.Path -like "C:\MT5_scalper\*"`.
3. `bot.ps1` never references, stops, starts, or restarts `xauusd-detector` or `xauusd-backend`.
4. All paths derived from `$PSScriptRoot` — no hardcoded user paths except the MT5 portable path.

---

## Disconnect (keep bots alive after RDP logoff)

```powershell
.\ops\bot.ps1 disconnect
```

Uses `tscon` to transfer the active RDP session to the console so interactive Task Scheduler tasks and MT5 terminals stay alive. One disconnect covers both bots — they share the same Windows session.
