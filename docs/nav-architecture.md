# Nav architecture (v2) — menu-driven thin client

Design for turning the panel from a fixed 3-page glance display into a
**navigable, drill-down dashboard** that grows to arbitrary depth and new
integrations (PVE VMs/LXCs, TrueNAS/PBS detail, then UniFi, Plex, ...) without
reflashing the firmware for each one.

Status: **proposed, not built.** Foundation pass (dark mode, diagnostics page,
gesture handlers) shipped first; this builds on it.

## Principle (keeps the hard invariant)

The ESP32 stays dumb. It never learns what "a VM" or "a Plex library" is. The
**aggregator owns all structure and logic**; the device is a generic browser
that renders whatever screen it is handed and follows links. All TLS/auth/JSON
parsing stays in Python, exactly as today.

## The two modes

- **Glance mode** (today's behavior): the auto-cycling CLUSTER / POOLS / BACKUPS
  summary. Stays as the idle/home view.
- **Browse mode** (new): tap in to explore a tree. Swipe/scroll a list, tap a row
  to drill down, back-gesture to pop. Idle timeout returns to glance mode.

## Aggregator: a screen/navigation API

Add a generic endpoint next to `/api/status`:

```
GET /ui/<path>      ->  a "screen" descriptor
```

Screen shape (proposed):

```json
{
  "title": "Mox-I7",
  "path": "pve/node/Mox-I7",
  "parent": "pve/cluster",
  "rows": [
    {"label": "Status",      "value": "online",  "state": "ok"},
    {"label": "CPU",         "value": "4%",       "state": "ok"},
    {"label": "vm 101 web",  "value": "running",  "state": "ok",  "drill": "pve/vm/101"},
    {"label": "ct 210 dns",  "value": "stopped",  "state": "warn", "drill": "pve/lxc/210"}
  ]
}
```

- `state` ∈ {ok, warn, crit, muted} → drives row color (reuses the theme palette).
- `drill` present → the row is a link; the device fetches `/ui/<drill>` on tap.
- `parent` → where the back-gesture goes (or the device just pops its own stack).
- Top-level `GET /ui/` (home) lists the providers as drillable rows: Cluster,
  Pools, Backups, (later) UniFi, Plex.

Keep it TTL-cached like `/api/status`. The device may fetch a screen per drill;
that's fine on the LAN.

## Aggregator: providers

Each integration implements a tiny interface and registers a home row:

```python
class Provider:
    id: str                       # "pve", "truenas", "pbs", "unifi", "plex"
    def home_row(self) -> Row: ...            # the entry on GET /ui/
    def screen(self, subpath: str) -> Screen: # handle /ui/<id>/<subpath>
```

- `pve` provider: `cluster` (nodes list) → `node/<name>` (detail + its VMs/LXCs
  via `/cluster/resources?type=vm`) → `vm/<id>` / `lxc/<id>` (status detail).
- `truenas`, `pbs`: pool/dataset and datastore/GC/task detail.
- Adding **Plex** later = a new `Provider` subclass + one home row. No firmware
  change. This is the whole point.

Reuse the existing async fetch functions; do NOT bolt on new clients (per
CLAUDE.md). Providers extend those.

## Device: the generic browser

- One reusable LVGL "list screen": a title + N rows (label left, value right,
  color by `state`), plus a selection cursor and a small scroll indicator.
- A **nav stack** of paths on the device. Drill = push + fetch `/ui/<drill>`.
  Back = pop + re-fetch (or cache the parent screen).
- Interaction (building on the foundation-pass gestures):
  - swipe up/down or tap a row = move cursor / select
  - tap (or swipe left) on a linked row = drill in
  - back-gesture (swipe right at depth 0-of-row, or a dedicated corner) = pop
  - idle timeout = drop back to glance mode
- Rows per screen are variable, so the list widget must be built dynamically
  (LVGL list/table, or a fixed pool of ~8 row widgets reused per screen — TBD,
  the ~8-widget pool avoids per-drill allocation churn on the no-PSRAM board).

## Open questions to settle before building

1. **Row rendering**: LVGL `table`/`list` vs. a reused fixed pool of row widgets.
   Lean toward the fixed pool (no-PSRAM, avoids fragmentation).
2. **How the device fetches a screen**: extend the existing 15s poll, or an
   on-demand fetch triggered by drill/back. Browse mode wants on-demand.
3. **Back navigation gesture** that doesn't collide with swipe-page nav.
4. **Caching/staleness**: show last-known + a refresh, or block on fetch.
5. **Payload size**: a node with many VMs — paginate rows or scroll.

## Rollout order (agreed 2026-07-01)

1. Generic list/detail browser + `/ui/` API, PVE first:
   cluster → node → VM/LXC → detail.
2. TrueNAS + PBS depth (pools/datasets, datastores/GC/tasks). **Priority over
   UniFi/Plex.**
3. UniFi controller integration (provider).
4. Plex (provider). Then "then then then..."

## Why now

We stopped at 3 hardcoded pages before hand-coding a 4th. Every future view and
integration would otherwise be bespoke LVGL + a reflash. This model makes new
features a Python provider and leaves the firmware stable.
