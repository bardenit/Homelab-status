# Homelab Status Panel (CYD + FastAPI)

A glanceable homelab dashboard on the ESP32-2432S028R ("Cheap Yellow Display").
A small FastAPI service aggregates Proxmox VE, TrueNAS, and Proxmox Backup
Server into one flat JSON payload; the CYD polls it and renders cluster, pool,
and PBS health with LVGL. RGB LED goes red on any alert.

Flat layout, everything at the repo root (no `aggregator/` or `esphome/` subdirs):

```
app.py               FastAPI service: /api/status, /api/unifi, /ui, /firmware, /admin
backends.py          shared plumbing (TrueNAS WebSocket, PVE/PBS/UniFi helpers, pooled client)
ui.py                /ui drill-down screen providers
admin.py, probe.py   admin config UI + per-source Test probes
firmware.py          OTA firmware hosting for the panels
config.py            /data/config.json config overlay
templates/           Jinja templates for the admin UI
tests/               mock-mode smoke tests
Dockerfile, docker-compose.yml, requirements*.txt
homelab-panel.yaml   ESPHome config for the CYD
secrets.yaml.example
docs/
```

## What it shows

- Cluster: per-node up/down + CPU% + RAM%, plus quorum state in the header.
- Storage: each TrueNAS pool as a bar, colored by health (green / amber near-full
  / red if not ONLINE). The DEGRADED case is loud on purpose.
- PBS: datastore usage (the GC-on-near-full-pool footgun, made visible).
- Network: live WAN up/down throughput from UniFi (fast 1s `/api/unifi` lane).
- Alert: any node down, quorum lost, pool not ONLINE, or anything over threshold
  trips the alert flag and the red LED.
- Drill-down: tap a card to browse detail screens (`/ui`) served by the aggregator
  (node, pool, datastore, gateway), so the panel stays a dumb list/detail browser.

## 1. Aggregator

Read-only tokens are all you need. Grant the minimum:

- Proxmox VE: API token with `PVEAuditor` on `/`. Token string `user@realm!id=SECRET`.
- TrueNAS: an API key (System > API Keys on SCALE).
- PBS: API token with `Datastore.Audit` on `/datastore`. **Token separator is a
  colon**, not `=` like PVE: `user@pbs!id:SECRET`.

```bash
cp .env.example .env      # fill in hosts + tokens (or just seed defaults)
docker compose up -d --build
curl http://localhost:8000/api/status      # sanity check
```

The `.env` only seeds first-run defaults. After it is up, open
`http://localhost:8000/admin`: set an admin password on the first visit, then
enter hosts/tokens (and UniFi host/key/site) in the form, with a per-source Test
button. Config persists to `/data/config.json` and takes effect on the next poll,
no restart. Everything under `/admin` is password-gated; `/api/status` and
`/healthz` stay public (the CYD cannot log in).

The container runs hardened (non-root uid 1000, `cap_drop: ALL`,
`no-new-privileges`, read-only rootfs, tmpfs `/tmp`). The `./data` volume holds
the only writable state, so once on the host run `chown -R 1000:1000 ./data`.

Run the mock-mode smoke tests with no backends:
```bash
docker run --rm -v "$PWD":/src -w /src python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt; python -m pytest tests/ -q"
```

Test the CYD before you have creds wired by setting `MOCK=1` in `.env`: the
service serves realistic fake data (including a down node and a degraded pool)
so you can verify the display end to end.

Self-signed certs on PVE/TrueNAS/PBS are expected and handled (verify disabled).
The endpoint is cached for `CACHE_TTL` seconds, so the panel can poll freely.

## 2. CYD firmware (ESPHome)

```bash
cp secrets.yaml.example secrets.yaml      # wifi creds
# edit homelab-panel.yaml (repo root): set agg_url to your aggregator host:port
esphome config homelab-panel.yaml         # VALIDATE FIRST
esphome run homelab-panel.yaml            # flash over USB the first time
```

After the first USB flash, panels self-update over WiFi: the aggregator hosts an
OTA binary at `/firmware` (uploaded from the `/admin` firmware page) and the
panels poll its manifest.

### Things to verify on first flash

- **Panel variant.** Most 2432S028R boards are ILI9341 (the default). Some later
  runs ship ST7789; if the screen is blank or garbled, switch `model:` to
  `ST7789V`. There are also two board revisions with different USB chips and a
  few moved pins, so double-check yours before wiring the spare CN1 connector.
- **Touch calibration.** The `calibration:` values are starting points; adjust
  after a first flash if taps land off. (Touch isn't used for anything yet, but
  it's wired so you can add page-switching later.)
- **LDR dimming direction.** On this board higher ADC voltage usually means
  darker; if dimming runs backwards, flip the mapping in the `ldr` `on_value`.

## Extending

Multi-page nav, touch drill-down (`/ui` providers), UniFi, OTA, and the PBS
`gc_age_h` readout are already in. Still open:

- Point the green LED to an "all good" state by flipping the `else` branch.
- Add a "last updated" age / stale indicator so a frozen panel is obvious.
- Add new integrations as `/ui` providers in `ui.py` (no firmware change needed).
