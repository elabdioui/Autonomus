# Copied from xauusd-bot

Source repo: `C:\Users\elabd\Desktop\Bot\xauusd-bot`
Commit hash at copy time: `e252d584fd0241533c2521d8465c7f758c34f55d`

## Files copied / adapted

| Source path | Destination | Notes |
|---|---|---|
| `detector/indicators/fvg.py` | `indicators/fvg.py` | No changes — already excludes forming candle |
| `detector/indicators/structure.py` | `indicators/structure.py` | **Rewritten** — `find_swings` now uses `confirmed_index` (look-ahead fix); `detect_structure_breaks` uses `confirmed_index` as reference pointer |
| `detector/strategy/scoring.py` | `core/scoring.py` | Decoupled from source `cfg`; weights passed as argument |
| `detector/strategy/killzone.py` | `core/sessions.py` | ASIA window changed from 01:00–05:00 UTC → 23:00–03:00 UTC per SPEC 0; midnight-crossing handled explicitly |
| `detector/mt5_client.py` | `mt5_client.py` | `connect()` now passes `path=MT5_TERMINAL_PATH`; exits on missing path, failed init, or login mismatch; `get_spread_pips()` added |
| `detector/config.py` | `config.py` | Rewritten for SPEC 0 §6 keys; dotenv loaded from project root |
| `detector/strategy/tier_a.py` (`get_asia_range`) | `indicators/asia_range.py` | Extracted as standalone module |
| `detector/indicators/liquidity.py` (`detect_regime`) | `indicators/regime.py` | Extracted as standalone; return value `"trend"` renamed to `"normal"` to match SPEC 0 §4 `vol_regime` enum |

Any future divergence from the source is intentional. Do not sync back automatically.
