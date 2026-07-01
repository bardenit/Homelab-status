# Session handoff

Running notes; newest first. Pick up cold from the top.

## 2026-07-01

- **Foundation pass shipped** (fw `2026.07.01-1`, flashed to `homelab-panel-04be24`):
  - **Dark mode** default; **long-press** toggles dark/light; persists across reboot.
  - **Diagnostics page** (**double-tap** to toggle; skipped by swipe/auto-cycle):
    panel id + fw, SSID, signal dBm, IP, BSSID, MAC, uptime. Uses ESPHome
    `wifi_info` / `wifi_signal` / `uptime`.
  - **Gestures**: swipe (right=next, left=prev), double-tap=diag, long-press=theme.
    Tuned for the resistive panel's chatter — a **350ms post-swipe cooldown** drops
    chatter fragments so a swipe can't fake a double-tap.
  - **WiFi home fix**: home SSID set `hidden: false` (Jason-Sanitized broadcasts).
- **v2 design drafted**: `docs/nav-architecture.md` — menu-driven thin client
  (aggregator serves navigable screens; device is a generic list/detail browser;
  integrations are Python providers). Rollout: PVE drill-down → TrueNAS/PBS depth
  → UniFi → Plex.

### Still open after today
- [ ] **Re-test home WiFi.** `hidden:false` is fixed, but the home WLAN is
  **WPA2/WPA3 + PMF Optional** — the classic ESP32 blocker (UniFi warns about it
  on that screen). If it still won't join, confirm via the diagnostics page, then
  make a **WPA2-only / PMF-disabled** IoT SSID for the panels.
- [ ] Build **v2 nav** (`docs/nav-architecture.md`), PVE drill-down first.
- [ ] Flash the remaining CYDs.
- [ ] PBS `gc_age_h` still null (see `todo.md`).

---

## 2026-06-29

Where we are and what's next, so we can pick this up cold tomorrow.

## What got done today

### Aggregator
- **Migrated TrueNAS off the deprecated REST API to JSON-RPC over WebSocket**
  (`wss://10.10.10.167/api/current`, `auth.login_with_api_key` → `pool.query`).
  REST `/api/v2.0` is removed in TrueNAS 26.04 and was already returning **403**
  for the Readonly-Admin key on 25.10. WebSocket honors the role fine.
  Both `fetch_truenas` (app.py) and `probe_truenas` (probe.py) now use it.
- Verified live against the real box (4 pools, all ONLINE).
- Committed to `main` and the multi-arch image is on Docker Hub (see below).

### Deployment
- Image `jbarden75/lab_health:latest` is **multi-arch** (amd64 + arm64). The LXC
  at `10.10.10.14` is **amd64**; Jason's Mac is arm64. Always build with
  `docker buildx build --platform linux/amd64,linux/arm64 ... --push`.
- LXC has **no git clone**; it deploys via `docker-compose pull && docker-compose up -d`.
- `/api/status` confirmed healthy: pve+truenas+pbs all true, alert false.

### PBS fix
- PBS showed "Connected — 0 datastores" because the **API token had no
  Datastore.Audit ACL** (PBS tokens carry their own ACL, separate from the user).
  Granted `DatastoreAudit` on `/datastore` → both datastores now report.

### CYD #1 firmware (the big one)
- Flashed `homelab-panel-04be24` (panel id = last 6 of MAC). Settled hardware:
  - **Driver: ST7789V** (not ILI9341), **`invert_colors: false`**, **light theme**
    (white bg `0xFFFFFF`, black text). Anything else looked washed/garbled.
  - **No PSRAM** → `buffer_size: 25%` (a full-screen LVGL buffer OOMs).
  - **Backlight pinned to full** in `on_boot`: the LDR is **covered by the case**,
    so auto-dim is disabled.
  - **CH340 serial, 115200 only** (set via `platformio_options: upload_speed`).
- **WiFi: two networks, hidden** — JB (work) and Jason-Sanitized (home, creds now
  in `secrets.yaml`). `power_save_mode: none`.
- **New multi-page UI**: 3 pages (CLUSTER / POOLS / BACKUPS) with the PBS page
  finally rendering. Swipe left/right (tracked via touchscreen `on_update`, not
  `on_touch`), auto-cycle every 5s after 10s idle, `page_wrap` on. Swipe works,
  direction correct.
- Panel self-reports `x-panel-id` + `x-panel-version` headers each poll.

## Open items / next steps

- [ ] **UniFi: disable Fast Roaming (802.11r) + BSS Transition on the JB WLAN.**
  The ESP32 can't do 802.11r and gets `Association Failed` / bounces. Firmware
  side is already mitigated (`power_save_mode: none`); this is the real fix.
- [ ] **Bring up the remaining CYD boards.** Same firmware, just flash each over
  USB once (then OTA after). Each self-IDs by MAC suffix. Watch for a board that
  is actually ILI9341 (flip `model:` + `invert_colors`).
- [ ] **Test the home WiFi** (Jason-Sanitized) when a board goes home. It's set
  `hidden: true`, which works whether or not it broadcasts.
- [ ] **PBS `gc_age_h` is null** (see todo.md). The BACKUPS page shows "GC --"
  until a GC runs or the token can read the GC task log on node `localhost`.
- [ ] Decide whether to bump `fw_version` per release (still `2026.06.29-1`).

## Key commands / paths

- Firmware: `homelab-panel.yaml` (repo root, NOT `esphome/` despite CLAUDE.md).
- esphome lives in `./.venv`: `./.venv/bin/esphome config homelab-panel.yaml`
  then `./.venv/bin/esphome run homelab-panel.yaml --device /dev/cu.usbserial-130 --no-logs`.
- Serial logs: macOS has no `timeout`; use a short pyserial read script with the
  ESP32 reset-into-run sequence (DTR low, pulse RTS) — see this session.
- Aggregator deploy: build multi-arch + push, then on the LXC
  `docker-compose pull && docker-compose up -d`.
- API details: `docs/aggregator-api.md`.

## Gotchas worth remembering

- TrueNAS = WebSocket only (REST dead in 26.04). PBS token separator is `:`,
  PVE is `=`. PBS tokens need their own Datastore.Audit ACL.
- This CYD: ST7789V / non-inverting / light theme / no PSRAM / CH340 115200.
- `secrets.yaml` is gitignored (real WiFi creds live there).
