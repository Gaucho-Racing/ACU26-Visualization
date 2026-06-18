#!/usr/bin/env python3
"""
ACU Pack Monitor - serial backend + live dashboard server.

Reads the battery-pack debug stream from a UART (default COM5 @ 2,000,000 baud),
parses each "Debug Start ... End Debug" frame into structured JSON, and pushes it
to the browser dashboard via Server-Sent Events. Also exposes a command endpoint
so the dashboard can write commands back into the UART.

Usage:
    python acu_server.py                 # live, reads COM5 @ 2000000
    python acu_server.py --port COM5 --baud 2000000
    python acu_server.py --demo          # no hardware: replay/animate serial.txt
    python acu_server.py --selftest      # parse serial.txt once and print JSON

Then open  http://localhost:8000  in your browser.
"""

import argparse
import json
import os
import queue
import random
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Strips the Arduino-monitor timestamp prefix:  "21:31:11.785 -> rest of line"
_PREFIX_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}\.\d+\s*->\s?")
_CELL_V_RE = re.compile(r"(\*?)C(\d+):\s*([-\d.]+)")
_TEMP_RE = re.compile(r"C(\d+):\s*([-\d.]+)")
_KV_RE = re.compile(r"([A-Za-z0-9+_]+)\s*:\s*([-\d.]+)")

# Relay tokens look like "IR+🟢" / "Prechg⚫".  Map the status glyph -> state.
_GLYPH_STATE = {
    "🟢": "on",
    "🔴": "fault",
    "🟡": "warn",
    "⚫": "off",
    "⚪": "off",
}


def _strip(line):
    return _PREFIX_RE.sub("", line.rstrip("\r\n"))


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_frame(lines):
    """Parse the list of (prefix-stripped) lines of one debug frame -> dict."""
    ics = {}  # index -> dict

    def ic(i):
        return ics.setdefault(
            i,
            {"index": i, "cells": [], "stack_voltage": None,
             "calc_stack": None, "board_temp": None, "cell_temps": []},
        )

    frame = {
        "state": None, "timestamp_ms": None, "relays": {},
        "ics": [], "summary": {}, "temp_summary": {},
        "adc": {}, "dcdc": {}, "bms_errors": None, "cps": None,
        "notes": [],
    }

    mode = None           # 'voltage' | 'temp'
    cur_ic = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if line.startswith("Timestamp"):
            m = re.search(r"Timestamp:\s*(\d+)", line)
            if m:
                frame["timestamp_ms"] = int(m.group(1))
            m = re.search(r"State:\s*(\S+)", line)
            if m:
                frame["state"] = m.group(1)
            continue

        # Relay line carries the status glyphs.
        if any(g in line for g in _GLYPH_STATE) and "IR" in line:
            # tokens separated by | -> "Name<glyph>"
            for tok in line.split("|"):
                tok = tok.strip()
                for g, st in _GLYPH_STATE.items():
                    if tok.endswith(g):
                        frame["relays"][tok[: -len(g)].strip()] = st
                        break
            continue

        if line.startswith("Cell Voltage"):
            mode = "voltage"
            continue
        if line.startswith("Cell Temp"):
            mode = "temp"
            continue

        # Section header "IC N:"  or  "IC N: 30.0" (board temp in temp mode)
        m = re.match(r"^IC\s+(\d+):\s*([-\d.]+)?\s*$", line)
        if m:
            cur_ic = int(m.group(1))
            obj = ic(cur_ic)
            if mode == "temp" and m.group(2) is not None:
                obj["board_temp"] = _num(m.group(2))
            continue

        # Cell voltage data rows  (have the optional * balancing marker)
        if mode == "voltage" and cur_ic is not None and "C" in line and ":" in line \
                and not line.startswith("Stack") and not line.startswith("Min"):
            obj = ic(cur_ic)
            for bal, idx, val in _CELL_V_RE.findall(line):
                obj["cells"].append({"i": int(idx), "v": _num(val),
                                     "bal": bal == "*"})
            continue

        if line.startswith("Stack Voltage") and cur_ic is not None:
            m1 = re.search(r"Stack Voltage:\s*([-\d.]+)", line)
            m2 = re.search(r"Calculated Stack:\s*([-\d.]+)", line)
            obj = ic(cur_ic)
            if m1:
                obj["stack_voltage"] = _num(m1.group(1))
            if m2:
                obj["calc_stack"] = _num(m2.group(1))
            continue

        # Voltage summary
        if line.startswith("Min:") and "Total" in line:
            kv = dict(_KV_RE.findall(line))
            frame["summary"] = {
                "min": _num(kv.get("Min")), "max": _num(kv.get("Max")),
                "total": _num(kv.get("Total")), "uv": _num(kv.get("UV")),
                "ov": _num(kv.get("OV")), "errors": _num(kv.get("Errors")),
            }
            continue

        # Temp cell rows
        if mode == "temp" and cur_ic is not None and line.startswith("C") and ":" in line:
            obj = ic(cur_ic)
            for idx, val in _TEMP_RE.findall(line):
                obj["cell_temps"].append(_num(val))
            continue

        if line.startswith("Min Temp"):
            kv = dict(_KV_RE.findall(line.replace("Min Temp", "MinT").replace("Max Temp", "MaxT")))
            frame["temp_summary"] = {
                "min": _num(kv.get("MinT")), "max": _num(kv.get("MaxT")),
                "errors": _num(kv.get("Errors")),
            }
            continue

        if line.startswith("ts_curr") or line.startswith("ts_volt"):
            frame["adc"] = {k: _num(v) for k, v in _KV_RE.findall(line)}
            continue

        if line.startswith("voltage") and "current" in line:
            frame["dcdc"] = {k: _num(v) for k, v in _KV_RE.findall(line)}
            continue

        if line.startswith("BMS Errors"):
            m = re.search(r"BMS Errors:\s*(\S+)", line)
            if m:
                frame["bms_errors"] = m.group(1)
            continue

        m = re.match(r"^(\d+)cps", line)
        if m:
            frame["cps"] = int(m.group(1))
            continue

    # finalize ics, ordered
    for i in sorted(ics):
        obj = ics[i]
        obj["cells"].sort(key=lambda c: c["i"])
        frame["ics"].append(obj)

    return frame


class FrameAssembler:
    """Feeds in raw serial lines; emits a parsed dict when a frame completes.

    A frame runs from one "Debug Start" up to (but not including) the next one,
    so trailing lines printed after "End Debug" -- e.g. "101cps" and
    "state override" -- are still captured and parsed.  Call flush() at EOF.
    """

    START = "Debug Start"

    def __init__(self, on_frame, on_line=None):
        self.on_frame = on_frame
        self.on_line = on_line
        self.buf = []
        self.started = False

    def _emit(self):
        if self.buf and self.started:
            try:
                self.on_frame(parse_frame(self.buf))
            except Exception as e:  # never let a bad frame kill the reader
                sys.stderr.write(f"[parse error] {e}\n")
        self.buf = []

    def feed_raw(self, raw_line):
        line = _strip(raw_line)
        if self.on_line:
            self.on_line(line)
        if self.START in line:
            self._emit()        # flush the previous frame
            self.started = True
            self.buf = []
            return
        if self.started:
            self.buf.append(line)

    def flush(self):
        self._emit()


# ---------------------------------------------------------------------------
# Hub: holds latest frame, fans out to SSE clients
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.latest = None
        self.writer = None  # callable(str) to push a command to the UART

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self.lock:
            self.clients.append(q)
            if self.latest is not None:
                q.put(self.latest)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, event):
        payload = json.dumps(event)
        with self.lock:
            self.latest = payload if event.get("type") == "frame" else self.latest
            for q in list(self.clients):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass


HUB = Hub()


def emit_frame(frame):
    HUB.publish({"type": "frame", "data": frame, "t": time.time()})


def emit_log(text, kind="rx"):
    HUB.publish({"type": "log", "kind": kind, "text": text, "t": time.time()})


# ---------------------------------------------------------------------------
# Serial reader
# ---------------------------------------------------------------------------

def serial_reader(port, baud):
    import serial  # imported here so --demo works without pyserial issues
    asm = FrameAssembler(emit_frame)
    while True:
        try:
            emit_log(f"Opening {port} @ {baud} ...", "sys")
            ser = serial.Serial(port, baud, timeout=1)
            HUB.writer = lambda s, _ser=ser: _ser.write((s + "\r\n").encode("utf-8", "replace"))
            emit_log(f"Connected to {port}", "sys")
            while True:
                raw = ser.readline()
                if not raw:
                    continue
                asm.feed_raw(raw.decode("utf-8", "replace"))
        except Exception as e:
            HUB.writer = None
            emit_log(f"Serial error: {e} -- retrying in 2s", "err")
            time.sleep(2)


# ---------------------------------------------------------------------------
# Demo mode: replay serial.txt and animate it like a live charge
# ---------------------------------------------------------------------------

def demo_reader(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    base = {"frame": None}
    asm = FrameAssembler(lambda fr: base.__setitem__("frame", fr))
    for ln in lines:
        asm.feed_raw(ln)
    asm.flush()
    base_frame = base["frame"]
    if base_frame is None:
        emit_log("Demo: no frame found in file", "err")
        return

    HUB.writer = lambda s: emit_log(f"(demo) -> would send: {s}", "tx")
    emit_log("Demo mode: animating serial.txt (no hardware).", "sys")

    t0 = time.time()
    soc_ramp = 0.0
    while True:
        fr = json.loads(json.dumps(base_frame))  # deep copy
        elapsed = time.time() - t0
        soc_ramp = min(0.12, elapsed * 0.0009)   # slow charge drift upward
        fr["timestamp_ms"] = int((base_frame.get("timestamp_ms") or 0) + elapsed * 1000)
        fr["cps"] = random.randint(98, 104)

        gmin, gmax = 9, 0
        for ic in fr["ics"]:
            for c in ic["cells"]:
                jitter = random.uniform(-0.002, 0.002)
                c["v"] = round((c["v"] or 3.0) + soc_ramp + jitter, 3)
                if random.random() < 0.02:
                    c["bal"] = not c["bal"]
                gmin = min(gmin, c["v"])
                gmax = max(gmax, c["v"])
            for k in range(len(ic["cell_temps"])):
                if ic["cell_temps"][k] is not None:
                    ic["cell_temps"][k] = round(ic["cell_temps"][k] + random.uniform(-0.2, 0.25) + elapsed * 0.002, 1)
            if ic.get("board_temp") is not None:
                ic["board_temp"] = round(ic["board_temp"] + random.uniform(-0.3, 0.3), 1)
            ic["stack_voltage"] = round(sum(c["v"] for c in ic["cells"]), 3)
            ic["calc_stack"] = ic["stack_voltage"]
        fr["summary"]["min"] = round(gmin, 3)
        fr["summary"]["max"] = round(gmax, 3)
        fr["summary"]["total"] = round(sum(ic["stack_voltage"] for ic in fr["ics"]), 3)
        if fr.get("adc"):
            fr["adc"]["ts_curr"] = round(-2.0 + random.uniform(-0.15, 0.15), 3)
            fr["adc"]["ts_volt"] = fr["summary"]["total"]
        emit_frame(fr)
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# HTTP server (dashboard + SSE + command endpoint)
# ---------------------------------------------------------------------------

class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browser tabs closing mid-SSE raise connection OSErrors; ignore them.
        if issubclass(sys.exc_info()[0] or Exception, OSError):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except FileNotFoundError:
                self._send(404, "text/plain", b"dashboard.html not found")
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = HUB.subscribe()
            try:
                while True:
                    try:
                        payload = q.get(timeout=15)
                        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")  # keep-alive
                    self.wfile.flush()
            except OSError:
                pass  # client disconnected (broken pipe / abort / reset)
            finally:
                HUB.unsubscribe(q)
            return

        self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/command":
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8", "replace")
            try:
                cmd = json.loads(body).get("cmd", "")
            except Exception:
                cmd = body
            cmd = cmd.strip()
            if not cmd:
                self._send(400, "application/json", b'{"ok":false,"err":"empty"}')
                return
            if HUB.writer is None:
                emit_log(f"No serial connection; dropped: {cmd}", "err")
                self._send(503, "application/json", b'{"ok":false,"err":"no serial"}')
                return
            try:
                HUB.writer(cmd)
                emit_log(cmd, "tx")
                self._send(200, "application/json", b'{"ok":true}')
            except Exception as e:
                emit_log(f"Send failed: {e}", "err")
                self._send(500, "application/json",
                           json.dumps({"ok": False, "err": str(e)}).encode())
            return
        self._send(404, "text/plain", b"not found")


def run_selftest(path):
    out = {}
    a = FrameAssembler(lambda fr: out.update(fr=fr))
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            a.feed_raw(ln)
    a.flush()
    print(json.dumps(out.get("fr", {}), indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--file", default=os.path.join(HERE, "serial.txt"))
    ap.add_argument("--demo", action="store_true",
                    help="replay/animate serial.txt instead of reading hardware")
    ap.add_argument("--selftest", action="store_true",
                    help="parse the file once, print JSON, exit")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        run_selftest(args.file)
        return

    if args.demo:
        threading.Thread(target=demo_reader, args=(args.file,), daemon=True).start()
    else:
        threading.Thread(target=serial_reader, args=(args.port, args.baud),
                         daemon=True).start()

    httpd = QuietServer(("127.0.0.1", args.http_port), Handler)
    url = f"http://localhost:{args.http_port}"
    mode = "DEMO" if args.demo else f"{args.port} @ {args.baud}"
    print(f"ACU monitor [{mode}]  ->  {url}   (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
