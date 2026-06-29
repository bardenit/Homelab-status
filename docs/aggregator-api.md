# Aggregator API Reference

How the aggregator talks to each backend, what it sends back to the CYD, and the
auth/permission gotchas that bit us. Written for future-us picking this back up.

The hard rule (see CLAUDE.md): the ESP32 never talks to PVE/TrueNAS/PBS. All TLS,
token auth, and JSON parsing happen here; the panel only ever sees the small
pre-digested `/api/status` payload.

---

## Topology (this environment)

| Thing                | Address              | Notes                                  |
|----------------------|----------------------|----------------------------------------|
| Aggregator (FastAPI) | `10.10.10.14:80`     | runs in Docker on the LXC              |
| Proxmox VE           | `10.10.10.11:8006`   | cluster "Moxie", 5 nodes               |
| TrueNAS SCALE        | `10.10.10.167`       | 25.10.x; JSON-RPC over WebSocket only  |
| Proxmox Backup Srv   | `10.10.10.15:8007`   | datastores: MainBackup, lil_Backup     |

Real hosts/tokens live in `/data/config.json` (gitignored volume), seeded by
`.env` on first run, editable at `/admin`. Secrets are never logged or echoed.

---

## Outbound calls (what the aggregator fetches)

Each source has one async fetch function in `app.py`. All run concurrently via
`asyncio.gather` behind a TTL cache. Any single source failing is caught, marked
`err=true`, trips `alert`, and still returns the rest of the payload (never 500s).

### Proxmox VE  — `fetch_pve()`  (REST)

- Base: `https://<pve_host>/api2/json`
- Auth header: `Authorization: PVEAPIToken=<token_id>=<secret>`  (note the `=`)
- Token role: **PVEAuditor** (read-only)

| Call | Purpose | Parsed |
|------|---------|--------|
| `GET /cluster/status` | quorum | item where `type=="cluster"` → `quorate` |
| `GET /cluster/resources?type=node` | per-node health | `status=="online"`→`up`; `cpu`=`round(cpu*100)`; `mem`=`pct(mem,maxmem)` |

Nodes are sorted by name. CPU/mem are forced to integer percents here so the
firmware parse stays trivial.

### TrueNAS  — `fetch_truenas()`  (JSON-RPC 2.0 over WebSocket)

- Endpoint: `wss://<truenas_host>/api/current`
- TLS is mandatory: TrueNAS revokes any API key presented over plain `ws://`.
- SSL verify is disabled (self-signed box).
- Auth: JSON-RPC method `auth.login_with_api_key` with params `["<key>"]` → `true`
- Key: a **user-linked** API key whose user has the **Readonly Admin** role.

| Call | Purpose | Parsed |
|------|---------|--------|
| `pool.query` | pool health + capacity | `name`, `status`, `healthy`; `used`=`pct(allocated,size)` |
| `pool.dataset.query` `[[["id","=",name]]]` | capacity fallback | `used.parsed` / `available.parsed` if `size`/`allocated` absent |

The tiny `_RpcConn` helper sends a JSON-RPC request with an incrementing `id` and
reads frames until it sees the matching reply (skipping event notifications).

> **Why WebSocket and not REST?** The legacy REST API (`/api/v2.0/...`) is
> deprecated in SCALE 25.04 and **removed in 26.04**. On 25.10 a Readonly-Admin
> key returned **403** on every REST call (`/pool`, `/system/info`) even though
> the same key authenticates fine over JSON-RPC. Migrating fixed it and
> future-proofs us. See the migration commit and `[[probe_truenas]]`.

### Proxmox Backup Server  — `fetch_pbs()`  (REST)

- Base: `https://<pbs_host>/api2/json`
- Auth header: `Authorization: PBSAPIToken=<token_id>:<secret>`  (note the `:`,
  **not** the `=` PVE uses — this trips people up)
- Token role: **Datastore.Audit** (read-only)

| Call | Purpose | Parsed |
|------|---------|--------|
| `GET /status/datastore-usage` | per-datastore usage | `store`, `total`, `used` → `used`=`pct(used,total)` |
| `GET /nodes/<node>/tasks?typefilter=garbage_collection&limit=1` | last GC age | `endtime` → `gc_age_h` (best-effort; failure leaves it null) |

> **"Connected — 0 datastores" gotcha.** PBS API **tokens carry their own ACL,
> separate from the user.** A token authenticates (so it looks "connected") but
> `/status/datastore-usage` returns an *empty list* (HTTP 200) if the token has
> no Datastore.Audit grant. Fix: PBS → Configuration → Access Control → Add API
> Token Permission → path `/datastore`, role `DatastoreAudit`, propagate.

---

## Inbound contract — `GET /api/status`

Public (the CYD can't log in). TTL-cached so the panel can poll freely. Shape:

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

All values the firmware consumes are integer percents. The firmware reads widgets
by array index, so **if you change field names or array shapes, update the parse
lambda in `homelab-panel.yaml` in the same change.**

Optional panel self-report: the firmware sends `x-panel-id` (6-char MAC suffix,
e.g. `04be24`) and `x-panel-version` headers on each poll; `record_checkin()`
logs which panel is on which firmware/IP for the admin UI. Never required.

`alert` is computed in `compute_alert()` and is true if any of: not quorate, a
node down, a node's mem ≥ `mem_warn`, a pool not `ok`, a pool used ≥ `pool_warn`,
a PBS datastore used ≥ `pbs_warn`, or any source down.

Also public: `GET /healthz` → `{"ok":true,"mock":<bool>}`.

---

## Connection probes — admin "Test" buttons (`probe.py`)

Unlike the fetch functions (which swallow errors and just mark a source down),
the probes surface the *real* reason a call failed (401 vs 403 vs connection
error) so the admin form shows actionable text. They test whatever values are
typed in, not what's persisted.

- `probe_pve` → `GET /cluster/status`
- `probe_truenas` → WebSocket `auth.login_with_api_key` then `pool.query` (kept in
  lockstep with `fetch_truenas`; do not let this drift back to REST)
- `probe_pbs` → `GET /status/datastore-usage`

`_explain()` maps HTTP status to human text: 401 = wrong token id/secret,
403 = authenticates but lacks read permission, 404 = wrong host/port/path.

---

## Caching & concurrency

- One `httpx.AsyncClient(verify=False)` per build; the three fetches run under
  `asyncio.gather`.
- `get_cached()` serves from `_cache` until `cache_ttl` elapses, with a lock +
  re-check after acquiring it to avoid a thundering herd on expiry.
- `invalidate_cache()` is called after a config save so `/admin` changes take
  effect on the next poll with no restart.
- `MOCK=1` (env or admin toggle) serves realistic fake data (a down node, a
  degraded pool, a near-full pool) with no backends.

---

## Quick test commands

```bash
# Aggregator payload (from anything on the LAN)
curl -s http://10.10.10.14/api/status | jq

# PVE token (note the = between id and secret)
curl -sk -H "Authorization: PVEAPIToken=<id>=<secret>" \
  https://10.10.10.11:8006/api2/json/cluster/status

# PBS token (note the : between id and secret)
curl -sk -H "Authorization: PBSAPIToken=<id>:<secret>" \
  https://10.10.10.15:8007/api2/json/status/datastore-usage

# TrueNAS is WebSocket, not curl. Minimal Python:
#   wss://10.10.10.167/api/current  (ssl verify off)
#   -> {"jsonrpc":"2.0","id":1,"method":"auth.login_with_api_key","params":["<key>"]}
#   -> {"jsonrpc":"2.0","id":2,"method":"pool.query","params":[]}
```

---

## Config keys (config.py)

`pve_host` / `pve_token_id` / `pve_secret`, `truenas_host` / `truenas_key`,
`pbs_host` / `pbs_token_id` / `pbs_secret` / `pbs_node`, plus tunables
`http_timeout`, `cache_ttl`, `mem_warn`, `pool_warn`, `pbs_warn`, `mock`.
