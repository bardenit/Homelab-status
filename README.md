# Homelab Status Panel (CYD + FastAPI)

A glanceable homelab dashboard on the ESP32-2432S028R ("Cheap Yellow Display").
A small FastAPI service aggregates Proxmox VE, TrueNAS, and Proxmox Backup
Server into one flat JSON payload; the CYD polls it and renders cluster, pool,
and PBS health with LVGL. RGB LED goes red on any alert.

```
homelab-panel/
  aggregator/          FastAPI service (runs in an LXC/Docker host)
    app.py
    requirements.txt
    Dockerfile
    .env.example
  esphome/
    homelab-panel.yaml ESPHome config for the CYD
    secrets.yaml.example
  docker-compose.yml
```

## What it shows

- Cluster: per-node up/down + CPU% + RAM%, plus quorum state in the header.
- Storage: each TrueNAS pool as a bar, colored by health (green / amber near-full
  / red if not ONLINE). The DEGRADED case is loud on purpose.
- PBS: datastore usage (the GC-on-near-full-pool footgun, made visible).
- Alert: any node down, quorum lost, pool not ONLINE, or anything over threshold
  trips the alert flag and the red LED.

## 1. Aggregator

Read-only tokens are all you need. Grant the minimum:

- Proxmox VE: API token with `PVEAuditor` on `/`. Token string `user@realm!id=SECRET`.
- TrueNAS: an API key (System > API Keys on SCALE).
- PBS: API token with `Datastore.Audit` on `/datastore`. **Token separator is a
  colon**, not `=` like PVE: `user@pbs!id:SECRET`.

```bash
cd aggregator
cp .env.example .env      # fill in hosts + tokens
cd ..
docker compose up -d --build
curl http://localhost:8000/api/status      # sanity check
```

Test the CYD before you have creds wired by setting `MOCK=1` in `.env`: the
service serves realistic fake data (including a down node and a degraded pool)
so you can verify the display end to end.

Self-signed certs on PVE/TrueNAS/PBS are expected and handled (verify disabled).
The endpoint is cached for `CACHE_TTL` seconds, so the panel can poll freely.

## 2. CYD firmware (ESPHome)

```bash
cd esphome
cp secrets.yaml.example secrets.yaml      # wifi creds
# edit homelab-panel.yaml: set agg_url to your aggregator host:port
esphome config homelab-panel.yaml         # VALIDATE FIRST
esphome run homelab-panel.yaml            # flash over USB the first time
```

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

- Add a second LVGL page (touch to switch) for VM/LXC lists or per-pool detail.
- Add a `gc_age_h` readout to the PBS row (the aggregator already returns it).
- Point the green LED to an "all good" state by flipping the `else` branch.
