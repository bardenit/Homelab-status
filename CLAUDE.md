# CLAUDE.md — Homelab Status Panel

Context for Claude Code working in this repo. Read this first, then ask before
making assumptions about my environment.

## What this is

A glanceable homelab dashboard on an ESP32-2432S028R ("Cheap Yellow Display").
A small FastAPI service aggregates Proxmox VE, TrueNAS, and Proxmox Backup
Server into one flat JSON payload; the CYD polls it and renders cluster, pool,
and PBS health with LVGL. The RGB LED goes red on any alert.

Hard rule: the ESP32 never talks to PVE/TrueNAS/PBS directly. All TLS, token
auth, and JSON parsing live in the aggregator. The microcontroller only ever
sees the small pre-digested payload.

## Repo layout

Flat, no `aggregator/` or `esphome/` subdirs. The Python service and the
firmware live side by side at the repo root:

```
app.py                FastAPI service: /api/status sweep, /api/unifi fast lane,
                      /ui drill-down, /firmware OTA, TTL cache, mock mode, lifespan
backends.py           shared plumbing: TrueNAS JSON-RPC/WebSocket session,
                      PVE/PBS/UniFi header builders + api_get, UniFi site/gateway
                      resolution, formatting helpers, one pooled httpx client
ui.py                 /ui/<path> drill-down screen providers (PVE, TrueNAS, PBS, UniFi)
admin.py              /admin web UI: first-run password, login, config form, firmware upload
probe.py              per-source connection probes behind the admin Test buttons
firmware.py           /firmware OTA manifest + binary hosting, per-panel version check-ins
config.py             runtime config: /data/config.json overlays env vars (fails loud)
templates/            Jinja templates for the admin UI
tests/test_smoke.py   mock-mode smoke tests (payload contract, admin gating, config)
requirements.txt      runtime deps
requirements-dev.txt  dev deps (pytest), pulls in requirements.txt
Dockerfile
docker-compose.yml
.env                  seeds defaults on first run; admin UI owns config after
homelab-panel.yaml    CYD firmware (ESPHome: two SPI buses, LVGL, LDR dim, RGB LED)
secrets.yaml.example  wifi creds
docs/                 handoff notes, nav architecture, API reference
README.md
```

## How to run and test

Aggregator:
```bash
cp .env.example .env                      # fill in hosts + tokens
docker compose up -d --build
curl http://localhost:8000/api/status
```
Set `MOCK=1` in `.env` to serve realistic fake data (down node, degraded pool,
near-full pool) with no backends, for testing the display end to end.

Tests (mock-mode smoke suite, no backends needed):
```bash
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt; python -m pytest tests/ -q"
```

The container runs hardened: non-root uid 1000, `cap_drop: ALL`,
`no-new-privileges`, read-only rootfs, tmpfs `/tmp`. The `./data` volume must be
owned by uid 1000, so once on the host run `chown -R 1000:1000 ./data`.

Config after first run: open `http://localhost:8000/admin`, set an admin
password (first visit only), then enter hosts/tokens in the form. These persist
to `/data/config.json` (a mounted volume) and take effect on the next poll, no
restart. The `.env` values only seed the initial form; the JSON file wins once
it exists. `/api/status` and `/healthz` stay public (the CYD cannot log in);
everything under `/admin` is password-gated.

Firmware (`homelab-panel.yaml` at the repo root):
```bash
cp secrets.yaml.example secrets.yaml
# set agg_url in homelab-panel.yaml to the aggregator host:port
esphome config homelab-panel.yaml         # ALWAYS validate before flashing
esphome run homelab-panel.yaml
```
There is no ESPHome toolchain assumption baked in: if you change the LVGL block,
run `esphome config` to validate, since that syntax shifts across versions.

## Conventions and invariants (do not break these)

- Read-only tokens only: PVEAuditor on PVE, the `Audit` role at `/` on PBS
  (read-only; Datastore.Audit alone cannot see GC/task logs or list nodes, so the
  broader all-read-only Audit role is needed for the GC-age readout), a plain API
  key (Readonly Admin user) on TrueNAS. Never request write scopes.
- PBS API token separator is a colon (`user@pbs!id:SECRET`), unlike PVE's `=`.
- Per-source failure must degrade gracefully: mark that source false in
  `sources`, trip `alert`, and still return the rest of the payload. Never let
  one dead backend 500 the endpoint.
- Payload values the firmware consumes are integer percents. Keep parsing on the
  ESP32 trivial; do the math in Python.
- The endpoint is TTL-cached (`CACHE_TTL`). Keep it that way so the panel can
  poll freely.
- Config now persists to `/data/config.json` via the admin UI (env vars only
  seed defaults). This is the one writable-state exception: it holds real
  hosts/tokens plus the admin password hash, so `data/` is gitignored, the
  volume is the only place secrets live at rest, and `/admin` must stay
  password-gated. Do not log or echo token fields.
- My personal style: no em dashes in anything you write. Use commas, colons, or
  parentheses.

## Payload contract

`GET /api/status` returns:
```json
{
  "ts": 0,
  "quorate": true,
  "nodes":  [{"name":"", "up":true, "cpu":0, "mem":0}],
  "pools":  [{"name":"", "health":"ONLINE", "ok":true, "used":0}],
  "pbs":    [{"name":"", "used":0, "gc_age_h":0.0}],
  "sources": {"pve":true, "truenas":true, "pbs":true},
  "alert": false
}
```
The firmware reads widgets by index. If you change field names or array shapes,
update the parse lambda in `homelab-panel.yaml` in the same change.

## My environment (confirm with me, do not guess)

- Cluster "Moxie", 5 nodes. The display has 5 fixed node slots to match. If node
  count changes, resize both the LVGL slots and the parse loop.
- TrueNAS pools are auto-discovered by the aggregator (it iterates whatever
  `/pool` returns), but the display only shows the first 4. If I run more pools,
  add slots or add a second page (see backlog).
- PBS datastores are auto-discovered via `/status/datastore-usage`.
- Aggregator runs in an LXC / Docker host on my LAN. Ask me for the actual IPs
  for PVE (`:8006`), TrueNAS, PBS (`:8007`), and the aggregator host.

## Known hardware gotchas

- Panel variant: most 2432S028R are ILI9341 (the default). Some later runs are
  ST7789; if the screen is blank or garbled, switch `model:` to `ST7789V`.
- Two board revisions exist with different USB chips and a few moved pins. Verify
  before wiring anything to the spare CN1 connector.
- Touch (XPT2046) is on its own SPI bus, separate from the display. Calibration
  values in the YAML are starting points; tune after a first flash.
- LDR dimming: higher ADC voltage usually means darker on this board. If dimming
  runs backwards, flip the mapping in the `ldr` `on_value` lambda.

## Backlog (rough priority)

Done: touch-driven multi-page nav with drill-down (`/ui` providers for PVE,
TrueNAS, PBS, UniFi) and the PBS `gc_age_h` readout (last-GC on the datastore
detail screen).

1. "All good" green LED state (currently idle is dark; flip the `else` branch in
   the interval automation).
2. On-screen "last updated" age and a stale indicator if the aggregator poll
   fails, so a frozen panel is obvious.
3. Optional: expose Prometheus-style metrics from the aggregator for Grafana,
   reusing the same fetch functions.

## Working agreement

- Validate ESPHome configs (`esphome config`) before telling me to flash.
- When you touch the payload, update both sides (aggregator and parse lambda).
- Prefer small, testable changes. The aggregator has clean async fetch functions
  per source; add new data by extending those, not by bolting on a new client.
- Ask before adding a new dependency or a new running service.
