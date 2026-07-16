#!/usr/bin/env python3
"""Same-origin helper for the llama-swap rig dashboard.

Serves index.html and transparently proxies API traffic to llama-swap on
127.0.0.1:8080 so the browser can pull everything without CORS trouble.
Streams responses chunk-by-chunk so SSE (/api/events) works unbuffered.

Pure stdlib. No parsing, no state. Run: python3 serve.py [--port 8090]
"""
import argparse
import http.server
import json
import os
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

UPSTREAM = "http://127.0.0.1:8080"
# The loaded model's llama-server is reached DIRECTLY (not via llama-swap) so that
# reading /slots can never trigger a model load/swap. llama-swap assigns each model
# a fresh port (${PORT}), so the port is resolved per-request from /running rather
# than hardcoded (see _current_upstream).
# Path prefixes proxied to llama-swap; everything else is served as a local file.
PROXY_PREFIXES = ("/api/", "/v1/", "/logs", "/running", "/health", "/unload", "/upstream", "/metrics")
HERE = os.path.dirname(os.path.abspath(__file__))
# Rig label shown in the page/tab title. Defaults to this machine's hostname;
# set RIG_NAME to override (e.g. a friendly name). The word "monitor" is kept as
# the suffix, so index.html's placeholder "rig monitor" becomes "<name> monitor".
RIG_NAME = os.environ.get("RIG_NAME", "").strip() or socket.gethostname()
RIG_LABEL = f"{RIG_NAME} monitor"

# Read-only SWE-bench results DB (written by ~/swebench-runs/ingest.py). Served at
# /_swebench (JSON) and /swebench (page). Opened read-only so serving can never
# lock or corrupt a DB that ingest.py / grade.sh may be writing concurrently.
SWEBENCH_DB = os.environ.get("SWEBENCH_DB", "/home/dwright/swebench-runs/results.db")

# ---------------------------------------------------------------------------
# Power / energy accounting
# ---------------------------------------------------------------------------
# A background thread samples power every SAMPLE_INTERVAL seconds and persists
# both a time-series and a running energy accumulator to sqlite, so the total
# keeps growing whether or not any browser tab is open (the old meter lived in
# the page's JS + localStorage and only accrued while the page was visible).
#
# Whole-system model (there is no wall meter on this box):
#   dc_w  = gpu_board + cpu_package(RAPL) + dram(RAPL) + BASE_W
#   sys_w = dc_w / PSU_EFF          # DC draw -> estimated AC draw at the wall
# GPU board power is measured (nvidia-smi via llama-swap). CPU+DRAM are measured
# from Intel RAPL energy counters. BASE_W (mobo/drives/fans/NIC) and PSU_EFF are
# the only estimated terms; tune via env if you ever get a Kill-A-Watt reading.
DB_PATH = os.path.join(HERE, "power.db")
SAMPLE_INTERVAL = 10.0                 # seconds between samples
RETAIN_DAYS = 30                       # trim time-series older than this
MAX_GAP_S = 300                        # don't integrate across gaps > this (suspend/downtime)
RAPL_PKG = "/sys/class/powercap/intel-rapl:0/energy_uj"    # CPU package (cores+uncore+igpu)
RAPL_DRAM = "/sys/class/powercap/intel-rapl:0:1/energy_uj"  # DRAM domain
BASE_W = float(os.environ.get("RIG_BASE_W", "30"))         # mobo/drives/fans/NIC baseline (DC W)
PSU_EFF = float(os.environ.get("RIG_PSU_EFF", "0.90"))     # PSU DC->AC efficiency
RATE_DEFAULT = 0.15                    # $/kWh until set from the UI
# Each sample is classified inference ("busy") vs idle from GPU utilization, so the
# UI can show where the cost goes. A sample counts as inference when any GPU's
# utilization is >= this %; below it the box is drawing power without doing work.
GPU_BUSY_UTIL = float(os.environ.get("RIG_BUSY_UTIL", "5"))


class PowerMeter:
    """Samples GPU (nvidia-smi via llama-swap) + CPU/DRAM (RAPL) power on a timer,
    integrates energy trapezoidally, and persists to sqlite. Thread-safe."""

    def __init__(self, db_path):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        self._init_schema()
        self.wrap = {"pkg": self._rapl_max(RAPL_PKG), "dram": self._rapl_max(RAPL_DRAM)}
        self.prev_rapl = None          # (ts, pkg_uj, dram_uj) for CPU/DRAM watt deltas
        self.prev_ts = None            # last integrated timestamp
        self.prev_gpu_w = None
        self.prev_sys_w = None
        self.latest = {}               # last computed snapshot (instantaneous watts)
        self._ticks = 0
        # resume the running totals across restarts from the newest stored sample
        row = self.db.execute(
            "SELECT gpu_wh, sys_wh, busy_wh, idle_wh, busy_s, idle_s "
            "FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        self.cum_gpu_wh = row[0] if row else 0.0
        self.cum_sys_wh = row[1] if row else 0.0
        # inference-vs-idle split of the SYSTEM energy/time. Invariant kept going
        # forward: cum_busy_wh + cum_idle_wh == cum_sys_wh. On a DB predating this
        # split (columns just added -> NULL), seed all prior energy as idle, since
        # the rig sits idle the vast majority of the time; the split then diverges
        # correctly from here (hit reset in the UI for a clean-slate breakdown).
        if row and row[2] is not None:
            self.cum_busy_wh, self.cum_idle_wh = row[2], row[3]
            self.cum_busy_s, self.cum_idle_s = row[4] or 0.0, row[5] or 0.0
        else:
            self.cum_busy_wh, self.cum_idle_wh = 0.0, self.cum_sys_wh
            self.cum_busy_s, self.cum_idle_s = 0.0, 0.0
            # Align the split's reset baseline with the existing system baseline so
            # the breakdown sums to the already-displayed "since reset" total right
            # away (the seed dumped ALL historical energy into idle above, including
            # energy from before the last reset — net it back out here). Only runs
            # the one time a pre-split DB is migrated.
            self.db.execute("UPDATE state SET v=0 WHERE k='reset_busy_wh'")
            self.db.execute("UPDATE state SET v=(SELECT v FROM state WHERE k='reset_sys_wh') "
                            "WHERE k='reset_idle_wh'")
            self.db.commit()

    # ---- setup -----------------------------------------------------------
    def _init_schema(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS samples(
                ts     INTEGER PRIMARY KEY,   -- unix seconds
                gpu_w  REAL, cpu_w REAL, dram_w REAL, sys_w REAL,
                gpu_wh REAL, sys_wh REAL,     -- cumulative running totals at this ts
                busy   INTEGER,               -- 1 = inference (GPU util >= threshold) this sample
                busy_wh REAL, idle_wh REAL,   -- cumulative system Wh split by inference/idle
                busy_s  REAL, idle_s  REAL    -- cumulative seconds split by inference/idle
            );
            CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v REAL);
            """
        )
        # migrate DBs created before the inference/idle split existed
        have = {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}
        for col, decl in (("busy", "INTEGER"), ("busy_wh", "REAL"), ("idle_wh", "REAL"),
                          ("busy_s", "REAL"), ("idle_s", "REAL")):
            if col not in have:
                self.db.execute(f"ALTER TABLE samples ADD COLUMN {col} {decl}")
        for k, v in (("rate", RATE_DEFAULT), ("reset_gpu_wh", 0.0),
                     ("reset_sys_wh", 0.0), ("reset_ts", 0.0),
                     ("reset_busy_wh", 0.0), ("reset_idle_wh", 0.0),
                     ("reset_busy_s", 0.0), ("reset_idle_s", 0.0)):
            self.db.execute("INSERT OR IGNORE INTO state(k, v) VALUES(?, ?)", (k, v))
        self.db.commit()

    # ---- readers ---------------------------------------------------------
    @staticmethod
    def _read_sysfs(path):
        """Read a sysfs file; if the udev rule hasn't made it readable, self-heal
        via passwordless sudo (chmod once, then fall back to `sudo cat`)."""
        try:
            with open(path) as f:
                return f.read().strip()
        except PermissionError:
            try:
                subprocess.run(["sudo", "-n", "chmod", "0444", path], timeout=3, check=False)
                with open(path) as f:
                    return f.read().strip()
            except Exception:
                r = subprocess.run(["sudo", "-n", "cat", path],
                                   capture_output=True, text=True, timeout=3)
                return r.stdout.strip() if r.returncode == 0 else None
        except OSError:
            return None

    def _rapl_max(self, energy_path):
        raw = self._read_sysfs(energy_path.replace("energy_uj", "max_energy_range_uj"))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _rapl_pair(self):
        def rd(p):
            raw = self._read_sysfs(p)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        return rd(RAPL_PKG), rd(RAPL_DRAM)

    @staticmethod
    def _wdelta(cur, prev, wrap):
        """Counter delta accounting for the RAPL wrap-around."""
        d = cur - prev
        if d < 0 and wrap:
            d += wrap
        return d if d >= 0 else 0

    def _gpu_sample(self):
        """Return (total_board_watts, busy) or (None, False). `busy` is True when
        any GPU is utilized past GPU_BUSY_UTIL, i.e. the rig is doing inference
        rather than sitting idle."""
        # primary source: llama-swap's performance ring (already server-side, no CORS)
        try:
            with urllib.request.urlopen(UPSTREAM + "/api/performance", timeout=3) as r:
                d = json.load(r)
            latest = {}   # keep only the newest row per GPU id (ring holds a history)
            for g in d.get("gpu_stats") or []:
                latest[g.get("id")] = g
            if latest:
                power = sum((g.get("power_draw_w") or 0.0) for g in latest.values())
                busy = any((g.get("gpu_util_pct") or 0.0) >= GPU_BUSY_UTIL
                           for g in latest.values())
                return power, busy
        except Exception:
            pass
        # fallback: query nvidia-smi directly (works even if no model is loaded)
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=power.draw,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                power = busy = 0.0
                for line in r.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 2:
                        continue
                    power += float(parts[0])
                    busy = busy or float(parts[1]) >= GPU_BUSY_UTIL
                return power, bool(busy)
        except Exception:
            pass
        return None, False

    # ---- sampling loop ---------------------------------------------------
    def tick(self):
        now = time.time()
        gpu_w, busy = self._gpu_sample()
        pkg, dram = self._rapl_pair()

        cpu_w = dram_w = None
        if self.prev_rapl and pkg is not None and self.prev_rapl[1] is not None:
            dt = now - self.prev_rapl[0]
            if dt > 0:
                cpu_w = self._wdelta(pkg, self.prev_rapl[1], self.wrap["pkg"]) / 1e6 / dt
                dram_w = self._wdelta(dram, self.prev_rapl[2], self.wrap["dram"]) / 1e6 / dt
        self.prev_rapl = (now, pkg, dram)

        if gpu_w is None:
            return  # no usable sample; don't integrate a hole
        cpu = cpu_w or 0.0
        drm = dram_w or 0.0
        sys_w = (gpu_w + cpu + drm + BASE_W) / PSU_EFF

        if self.prev_ts is not None:
            dt = now - self.prev_ts
            if 0 < dt < MAX_GAP_S:
                self.cum_gpu_wh += (self.prev_gpu_w + gpu_w) / 2 * dt / 3600
                pw = self.prev_sys_w if self.prev_sys_w is not None else sys_w
                inc_wh = (pw + sys_w) / 2 * dt / 3600
                self.cum_sys_wh += inc_wh
                # attribute this interval to inference or idle (keeps the invariant
                # cum_busy_wh + cum_idle_wh == cum_sys_wh going forward)
                if busy:
                    self.cum_busy_wh += inc_wh
                    self.cum_busy_s += dt
                else:
                    self.cum_idle_wh += inc_wh
                    self.cum_idle_s += dt
        self.prev_ts, self.prev_gpu_w, self.prev_sys_w = now, gpu_w, sys_w

        snap = {"ts": now, "gpu_w": gpu_w, "cpu_w": cpu, "dram_w": drm, "sys_w": sys_w,
                "gpu_wh": self.cum_gpu_wh, "sys_wh": self.cum_sys_wh, "busy": busy}
        with self.lock:
            self.latest = snap
            self.db.execute(
                "INSERT OR REPLACE INTO samples"
                "(ts,gpu_w,cpu_w,dram_w,sys_w,gpu_wh,sys_wh,busy,busy_wh,idle_wh,busy_s,idle_s)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(now), gpu_w, cpu, drm, sys_w, self.cum_gpu_wh, self.cum_sys_wh,
                 1 if busy else 0, self.cum_busy_wh, self.cum_idle_wh,
                 self.cum_busy_s, self.cum_idle_s))
            self.db.commit()

    def run(self):
        while True:
            try:
                self.tick()
                self._ticks += 1
                if self._ticks % 360 == 0:      # ~hourly retention trim
                    cutoff = int(time.time() - RETAIN_DAYS * 86400)
                    with self.lock:
                        self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                        self.db.commit()
            except Exception as e:
                print("power tick error:", e, file=sys.stderr, flush=True)
            time.sleep(SAMPLE_INTERVAL)

    # ---- accessors for the HTTP endpoints --------------------------------
    def snapshot(self):
        with self.lock:
            s = dict(self.latest)
            rate = self._state("rate")
            gpu_wh = max(0.0, self.cum_gpu_wh - self._state("reset_gpu_wh"))
            sys_wh = max(0.0, self.cum_sys_wh - self._state("reset_sys_wh"))
            busy_wh = max(0.0, self.cum_busy_wh - self._state("reset_busy_wh"))
            idle_wh = max(0.0, self.cum_idle_wh - self._state("reset_idle_wh"))
            busy_s = max(0.0, self.cum_busy_s - self._state("reset_busy_s"))
            idle_s = max(0.0, self.cum_idle_s - self._state("reset_idle_s"))
            since = self._state("reset_ts")
        return {
            "gpu_w": s.get("gpu_w"), "cpu_w": s.get("cpu_w"),
            "dram_w": s.get("dram_w"), "sys_w": s.get("sys_w"),
            "sample_ts": s.get("ts"), "busy": s.get("busy"),
            "gpu_wh": gpu_wh, "sys_wh": sys_wh,
            "rate": rate, "gpu_cost": gpu_wh / 1000 * rate, "sys_cost": sys_wh / 1000 * rate,
            # inference-vs-idle split of the system energy/cost/time since last reset
            "busy_wh": busy_wh, "idle_wh": idle_wh,
            "busy_cost": busy_wh / 1000 * rate, "idle_cost": idle_wh / 1000 * rate,
            "busy_s": busy_s, "idle_s": idle_s,
            "since_ts": since, "interval": SAMPLE_INTERVAL,
            "base_w": BASE_W, "psu_eff": PSU_EFF, "busy_util": GPU_BUSY_UTIL,
        }

    def set_rate(self, r):
        with self.lock:
            self.db.execute("UPDATE state SET v=? WHERE k='rate'", (r,))
            self.db.commit()

    def reset(self):
        with self.lock:
            self.db.execute("UPDATE state SET v=? WHERE k='reset_gpu_wh'", (self.cum_gpu_wh,))
            self.db.execute("UPDATE state SET v=? WHERE k='reset_sys_wh'", (self.cum_sys_wh,))
            self.db.execute("UPDATE state SET v=? WHERE k='reset_busy_wh'", (self.cum_busy_wh,))
            self.db.execute("UPDATE state SET v=? WHERE k='reset_idle_wh'", (self.cum_idle_wh,))
            self.db.execute("UPDATE state SET v=? WHERE k='reset_busy_s'", (self.cum_busy_s,))
            self.db.execute("UPDATE state SET v=? WHERE k='reset_idle_s'", (self.cum_idle_s,))
            self.db.execute("UPDATE state SET v=? WHERE k='reset_ts'", (time.time(),))
            self.db.commit()

    def _state(self, k):
        row = self.db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return row[0] if row else 0.0


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    meter = None  # PowerMeter, set in main()

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _is_proxied(self):
        return any(self.path == p or self.path.startswith(p) for p in PROXY_PREFIXES)

    def _current_upstream(self):
        """Base URL of the currently-loaded llama-server, or None if none is
        running. llama-swap assigns each model a fresh ${PORT}, so read the live
        port from /running rather than hardcoding one. A GET to /running never
        triggers a swap."""
        try:
            with urllib.request.urlopen(UPSTREAM + "/running", timeout=2) as r:
                running = json.load(r).get("running") or []
        except Exception:
            return None
        return running[0].get("proxy") if running else None

    def _swebench(self):
        """Serve the SWE-bench results DB as JSON (read-only). Returns a friendly
        empty payload (200) if the DB is missing/locked so the page degrades nicely."""
        try:
            con = sqlite3.connect(f"file:{SWEBENCH_DB}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            try:
                runs = [dict(r) for r in con.execute("SELECT * FROM run_summary ORDER BY run_id")]
                results = [dict(r) for r in con.execute(
                    "SELECT run_id, instance_id, exit_status, n_steps, cost, patch_chars, "
                    "has_patch, resolved, error, empty_patch, graded FROM results "
                    "ORDER BY run_id, instance_id")]
                meta = dict(con.execute(
                    "SELECT COUNT(*) AS runs, "
                    "(SELECT COUNT(*) FROM results) AS instances FROM runs").fetchone())
            finally:
                con.close()
            self._json({"ok": True, "runs": runs, "results": results, "meta": meta})
        except Exception as e:
            self._json({"ok": False, "error": str(e), "runs": [], "results": [], "meta": {}})

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/_swebench":
            self._swebench()
            return
        if route == "/swebench" or route == "/swebench.html":
            self._serve_file("swebench.html")
            return
        if route == "/_slots":
            # live token counts, read straight from the loaded llama-server (never swaps a model)
            base = self._current_upstream()
            if not base:
                self.send_error(503, "no model loaded")
                return
            self._proxy("GET", target=base + "/slots")
        elif route == "/_energy":
            self._json(self.meter.snapshot() if self.meter else {})
        elif self._is_proxied():
            self._proxy("GET")
        elif route == "/" or route == "/index.html":
            self._serve_file("index.html")
        elif route == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404, "not found")

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route == "/_energy/rate":
            if not self.meter:
                self._json({"error": "no meter"}, 503); return
            r = self._read_json().get("rate")
            try:
                r = float(r)
            except (TypeError, ValueError):
                self._json({"error": "rate must be a number"}, 400); return
            if r < 0:
                self._json({"error": "rate must be >= 0"}, 400); return
            self.meter.set_rate(r)
            self._json(self.meter.snapshot())
        elif route == "/_energy/reset":
            if not self.meter:
                self._json({"error": "no meter"}, 503); return
            self.meter.reset()
            self._json(self.meter.snapshot())
        elif self._is_proxied():
            self._proxy("POST")
        else:
            self.send_error(404, "not found")

    def _serve_file(self, name):
        path = os.path.join(HERE, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "not found")
            return
        if name.endswith(".html"):
            # stamp the rig label into the title + header ("rig monitor" placeholder)
            body = body.replace(b"rig monitor", RIG_LABEL.encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method, target=None):
        length = int(self.headers.get("Content-Length", 0) or 0)
        payload = self.rfile.read(length) if length else None
        req = urllib.request.Request(target or (UPSTREAM + self.path), data=payload, method=method)
        for h in ("Content-Type", "Authorization", "Accept"):
            if h in self.headers:
                req.add_header(h, self.headers[h])
        try:
            # bounded timeout for direct upstream calls; unbounded for the SSE stream
            resp = urllib.request.urlopen(req, timeout=(5 if target else None))
        except urllib.error.HTTPError as e:
            resp = e  # forward upstream error bodies verbatim
        except Exception as e:
            self.send_error(502, f"upstream unreachable: {e}")
            return

        self.send_response(resp.status)
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        is_stream = "text/event-stream" in ctype
        self.send_header("Content-Type", ctype)
        if is_stream:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self._pump(resp)
        else:
            body = resp.read()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _pump(self, resp):
        """Relay an SSE stream line-by-line until the client goes away."""
        try:
            for line in resp:            # yields on each newline as data arrives
                self.wfile.write(line)
                if line == b"\n":         # blank line = end of an SSE event -> flush
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    # start the always-on power sampler (survives browser tabs being closed)
    try:
        Handler.meter = PowerMeter(DB_PATH)
        threading.Thread(target=Handler.meter.run, daemon=True).start()
        print(f"power sampler on -> {DB_PATH} (every {SAMPLE_INTERVAL:.0f}s)", flush=True)
    except Exception as e:
        print(f"power sampler disabled: {e}", file=sys.stderr, flush=True)

    srv = Server((args.host, args.port), Handler)
    print(f"dashboard on http://{args.host}:{args.port}  ->  proxying {UPSTREAM}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)


if __name__ == "__main__":
    main()
