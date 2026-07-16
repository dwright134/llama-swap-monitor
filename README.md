# llama-swap rig monitor

A single-page, real-time monitoring dashboard for a [llama-swap](https://github.com/mostlygeek/llama-swap) server. It shows the running model, live token throughput, context-window usage, per-GPU and system resources, and a streaming log viewer — all in one screen with a dark terminal aesthetic.

![dashboard](docs/screenshot.png)

<!-- Drop a screenshot at docs/screenshot.png if you want the image above to render. -->

## What it shows

- **Model serving** — the currently loaded model, its state (serving / loading / idle), and configured context size.
- **Prompt / decode throughput** — live tokens/sec while a request is generating; `0` when idle.
- **Session tokens** — input + output of the current request (tracks the live conversation; resets when the client clears context).
- **Context window** — a color-coded bar of current KV occupancy vs the model's `--ctx-size` (green → yellow at 60% → red at 80%), with percent free.
- **System power** — estimated wall (AC) draw, plus an always-accumulating energy + cost total (`$/kWh`, global across browsers). **Hover the cost** for a donut breakdown of where the cost goes — **inference vs idle** — with each side's cost, share, kWh, and time.
- **Per-GPU cards** — utilization, VRAM, power (same 60/80% thresholds), plus temp / fan / memory-util.
- **System card** — CPU (per-core) and RAM.
- **Charts** — rolling GPU-utilization (per card) and decode-throughput history.
- **Live logs** — streamed proxy/upstream logs with level coloring, source filter, wrap toggle, and a **follow** button that pins to the newest lines.
- A **generating…** indicator that pulses while a request is in flight.

Values update live *during* generation — including while a model is "thinking" — by reading the llama-server slot state directly.

## How it works

The whole UI is one self-contained `index.html` (vanilla JS/CSS, no build step, no dependencies). It pulls data from llama-swap:

| Data | Source |
|------|--------|
| Model state, request metrics, streaming logs, in-flight count | `GET /api/events` (SSE) |
| GPU + CPU/RAM stats | `GET /api/performance` (polled every 5s) |
| Running model + `--ctx-size` | `GET /running` |
| Live per-token counts during generation | the llama-server `/slots` endpoint, read **directly** (see below) |

Because llama-swap does **not** send CORS headers on its responses, a browser can't fetch it cross-origin. So `serve.py` — a ~130-line, pure-stdlib helper — serves the page and transparently proxies API calls to llama-swap on the **same origin**. It has no dependencies, no state, and no configuration files.

### Why `/slots` is read directly (swap-safety)

Live token counts come from the llama-server `/slots` endpoint. It is **not** read through llama-swap's `/upstream/{model}/…` route, because that route runs the request through llama-swap's model router and will **load/swap to the targeted model if it isn't already running** — a monitor polling it could ping-pong models against your real workload. Instead, `serve.py` exposes `GET /_slots`, which reads the loaded llama-server **directly on its own port**, bypassing the swap logic entirely. llama-swap assigns each model a fresh port (`${PORT}`, from `startPort`), so `serve.py` resolves the live port from llama-swap's `/running` (a GET that never triggers a swap) rather than hardcoding one. When nothing is loaded it returns `503` and the UI falls back to the last completed request's numbers. Polling it can never trigger a swap.

## Requirements

- A running **llama-swap** with the `/api/performance` and `/api/events` endpoints (v224 or newer).
- GPU stats come from llama-swap's performance monitor (nvidia-smi / LACT / rocm-smi) — nothing to install here.
- **Python 3** (standard library only — no `pip install`).
- The llama-server upstream, whose port llama-swap assigns per-model via `${PORT}`; `serve.py` discovers it from `/running` (see `_current_upstream`), so no fixed upstream port is assumed.
- **(Optional) Intel RAPL access** for the whole-system power estimate — see the udev rule step below. Without it the sampler still runs and records GPU energy; it just can't add CPU/DRAM to the wall estimate (unless the service user has passwordless sudo, which it self-heals with).

## Install

```bash
git clone <this-repo> ~/dashboard
cd ~/dashboard
```

### Run it directly

```bash
python3 serve.py            # serves on 0.0.0.0:8090, proxying llama-swap at 127.0.0.1:8080
python3 serve.py --port 9000 --host 127.0.0.1
```

Then open `http://<rig-ip>:8090/`.

### Run it as a service (systemd, always-on)

A user unit is included. Edit the paths in `llama-dashboard.service` if you didn't clone to `~/dashboard`, then:

```bash
mkdir -p ~/.config/systemd/user
cp llama-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now llama-dashboard.service

# survive logout / run at boot without an active login session:
loginctl enable-linger "$USER"
```

Check it: `systemctl --user status llama-dashboard` · logs: `journalctl --user -u llama-dashboard -f`.

### (Optional) Enable whole-system power — Intel RAPL

The power tile estimates **wall (AC) draw** as `(GPU + CPU + DRAM + baseline) ÷ PSU efficiency`. CPU + DRAM come from Intel RAPL, whose `energy_uj` counters are root-only by default. A udev rule (included) makes them world-readable and persists across reboots:

```bash
sudo cp 99-rapl-readable.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=powercap --action=change
```

Skip this and the sampler still records GPU energy — it just drops the CPU/DRAM terms (or, if the service user has passwordless sudo, self-heals by reading them via `sudo`). Intel CPUs only.

### Inference-vs-idle cost breakdown

Every sample is tagged **inference** or **idle** from GPU utilization (any GPU busier than `RIG_BUSY_UTIL`, default 5% → inference), and the system energy for each interval is banked into the matching bucket. Hovering the cost line on the power tile shows the split as a donut — so you can see how much of the bill is real work vs. the box sitting powered-on. The invariant `busy + idle == total` is maintained, so the two slices always sum to the number on the tile.

The `power.db` schema is migrated automatically on startup (new columns are added in place — no manual step, and older `serve.py` still reads the DB fine). On an existing DB, energy accrued *before* this feature is all counted as idle; hit the ↺ reset once for a clean-slate breakdown going forward.

## Configuration

Everything is a small edit near the top of the two files:

- **Ports / hosts** — `serve.py`: `--port` / `--host` flags; `UPSTREAM` (llama-swap, `:8080`) constant. The llama-server upstream port is resolved dynamically from `/running` (llama-swap's per-model `${PORT}`), not a constant.
- **Rig name in the title** — the page/tab title reads `<name> monitor`, where `<name>` defaults to the machine's **hostname**. Set the `RIG_NAME` environment variable to override it (e.g. `RIG_NAME=gpubox python3 serve.py`, or uncomment the `Environment=` line in the systemd unit).
- **Bar thresholds** — `index.html`: GPU/system meters use `loadColor(pct, 60, 80)`; the context bar switches at 60% / 80%. Adjust to taste.
- **Poll intervals / history depth** — `index.html` top of `<script>`: `PERF_MS` (perf poll), `HIST` (util history points), `DEC_HIST`, `LOG_CAP`, and the `/slots` interval in `onInflight`.
- **Power bar scale** — `POWER_MAX` (per-GPU TDP in watts).
- **Whole-system power model** — `serve.py`: `BASE_W` (mobo/drives/fans baseline, DC watts) and `PSU_EFF` (PSU DC→AC efficiency) are the only *estimated* terms in the wall figure; override without editing via `RIG_BASE_W` / `RIG_PSU_EFF` env vars. Set them from a Kill-A-Watt reading to calibrate. `SAMPLE_INTERVAL` (sampler period) and `RETAIN_DAYS` (time-series retention) also live here. Accumulated energy + $/kWh rate persist in `power.db` (sqlite, gitignored) and are global across browsers; reset via the ↺ on the tile.
- **Inference-vs-idle threshold** — `serve.py`: `RIG_BUSY_UTIL` env var (default `5`) — the GPU-utilization percent above which a sample counts as *inference* rather than *idle* in the cost breakdown.

## Notes

- No authentication — it binds to `0.0.0.0` like llama-swap itself. Fine for a trusted LAN; **don't expose the port to the internet.**
- llama-swap swaps are always **request-driven**: it only loads a different model when a client asks for one. If you see frequent swapping, it's your client requesting multiple models (e.g. a main model plus a separate small/title model) — not this dashboard, which is swap-safe by design.
- `serve.py` streams the SSE log/event feed unbuffered so logs appear in real time.

## Files

- `index.html` — the entire dashboard (UI + all logic).
- `serve.py` — same-origin static server + swap-safe API proxy.
- `llama-dashboard.service` — systemd user unit.
- `99-rapl-readable.rules` — optional udev rule granting non-root read access to Intel RAPL energy counters (CPU/DRAM power).
