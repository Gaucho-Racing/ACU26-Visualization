# ⚡ ACU Pack Monitor

Live visualization + command console for the ACU-25 battery pack debug stream
(COM5 UART @ 2,000,000 baud).

## Run it

**Live (hardware on COM5):**
```
python acu_server.py
```
Opens `http://localhost:it i8000` in your browser automatically.

**Demo (no hardware — animates `serial.txt`):**
```
python acu_server.py --demo
```

**Other options:**
```
python acu_server.py --port COM5 --baud 2000000   # pick port/baud
python acu_server.py --http-port 8000             # change web port
python acu_server.py --selftest                   # parse serial.txt -> JSON, exit
python acu_server.py --no-open                     # don't auto-open browser
```
On Windows you can just double-click **`run.bat`** (live) or **`run-demo.bat`**.

First-time only: `python -m pip install pyserial`

## What you see
- **Pack overview** — voltage, current (charge ▼in / discharge ▲out), SoC estimate,
  cell Δ, min/max cell, max temp, live balancing count.
- **Cell matrix** — all 10 ICs × 14 cells as a heatmap. Toggle **Voltage / Temperature**.
  Cells actively balancing (`*` in the firmware) get a pulsing ⚡ outline. Hover any cell
  for exact V / °C.
- **Trends** — rolling charts of pack voltage, current, max temp, cell Δ.
- **Relays & SDC** — IR+, IR-, Precharge, SDC lamps + 12V rail, SDC voltage, water sense, DCDC.
- **Faults** — BMS error code, voltage/temp error counts, UV/OV limits.

## Command console (matches the firmware's LPUART parser)
Commands are newline-terminated. **State changes require state-override first.**

| Button / command        | Effect                                              |
|-------------------------|-----------------------------------------------------|
| `override -s 1` / `-s 0`| enable / disable **state** override                 |
| `override -e 1` / `-e 0`| enable / disable **error** override                 |
| `state -i`              | INIT                                                |
| `state -p`              | PRECHARGE                                           |
| `state -c <N>`          | CHARGE at `N` **amps** (0–14 A). The CHARGE button uses the slider, e.g. 10 A → `state -c 10` |
| `state -n`              | NORMAL                                              |
| `state -s`              | SHUTDOWN                                            |

Typical charge bring-up: **🔓 Override ON** → set slider → **CHARGE**.

You can also type any raw command in the input box and press Enter.

## Files
- `acu_server.py` — serial reader, frame parser, web server (SSE + command POST).
- `dashboard.html` — the visualization (vanilla JS/Canvas, no internet needed).
- `serial.txt` — sample capture used by `--demo` / `--selftest`.
