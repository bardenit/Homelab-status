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

```
aggregator/
  app.py                FastAPI service (async httpx, TTL cache, MOCK mode)
  requirements.txt
  Dockerfile
  .env.example          all config is env vars; no secrets in code
esphome/
  homelab-panel.yaml    CYD firmware (two SPI buses, LVGL, LDR dim, RGB LED)
  secrets.yaml.example  wifi creds
docker-compose.yml
README.md
```

## How to run and test

Aggregator:
```bash
cd aggregator && cp .env.example .env     # fill in hosts + tokens
cd .. && docker compose up -d --build
curl http://localhost:8000/api/status
```
Set `MOCK=1` in `.env` to serve realistic fake data (down node, degraded pool,
near-full pool) with no backends, for testing the display end to end.

Firmware:
```bash
cd esphome && cp secrets.yaml.example secrets.yaml
# set agg_url in homelab-panel.yaml to the aggregator host:port
esphome config homelab-panel.yaml         # ALWAYS validate before flashing
esphome run homelab-panel.yaml
```
There is no ESPHome toolchain assumption baked in: if you change the LVGL block,
run `esphome config` to validate, since that syntax shifts across versions.

## Conventions and invariants (do not break these)

- Read-only tokens only: PVEAuditor on PVE, Datastore.Audit on PBS, a plain API
  key on TrueNAS. Never request write scopes.
- PBS API token separator is a colon (`user@pbs!id:SECRET`), unlike PVE's `=`.
- Per-source failure must degrade gracefully: mark that source false in
  `sources`, trip `alert`, and still return the rest of the payload. Never let
  one dead backend 500 the endpoint.
- Payload values the firmware consumes are integer percents. Keep parsing on the
  ESP32 trivial; do the math in Python.
- The endpoint is TTL-cached (`CACHE_TTL`). Keep it that way so the panel can
  poll freely.
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
update the parse lambda in `esphome/homelab-panel.yaml` in the same change.

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

1. Second LVGL page with touch-to-switch (touch is wired but unused). Candidates:
   per-pool detail, or a VM/LXC up/down list pulled from PVE
   `/cluster/resources?type=vm`.
2. Show `gc_age_h` on the PBS row (already in the payload, not yet displayed).
   Color it amber if GC is older than N days.
3. "All good" green LED state (currently idle is dark; flip the `else` branch in
   the interval automation).
4. On-screen "last updated" age and a stale indicator if the aggregator poll
   fails, so a frozen panel is obvious.
5. Optional: expose Prometheus-style metrics from the aggregator for Grafana,
   reusing the same fetch functions.

## Working agreement

- Validate ESPHome configs (`esphome config`) before telling me to flash.
- When you touch the payload, update both sides (aggregator and parse lambda).
- Prefer small, testable changes. The aggregator has clean async fetch functions
  per source; add new data by extending those, not by bolting on a new client.
- Ask before adding a new dependency or a new running service.
