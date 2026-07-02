"""
Navigable UI screens for the panel's browse/drill-down mode.

The ESP32 stays dumb: it fetches GET /ui/<path> and renders a generic screen
(title + a list of rows). A row with a "drill" target is a link the panel
follows on tap; the panel keeps a back-stack of paths. All structure and logic
live here as "providers", so a new integration (UniFi, Plex, ...) is added in
Python with no firmware change. See docs/nav-architecture.md.

Screen shape:
  {"title": str, "path": str, "parent": str|None,
   "rows": [{"label": str, "value": str, "state": "ok|warn|crit|muted",
             "drill": str|None}]}
"""

import asyncio
import json
import ssl
import time
from urllib.parse import quote

import httpx
import websockets

import config


def _row(label: str, value: str = "", state: str = "ok", drill: str | None = None,
         cpu: int | None = None, mem: int | None = None) -> dict:
    r = {"label": label, "value": value, "state": state, "drill": drill}
    # cpu/mem present => the panel renders this row as a gauge card, not a stat line
    if cpu is not None:
        r["cpu"] = cpu
    if mem is not None:
        r["mem"] = mem
    return r


def _screen(title: str, rows: list, path: str = "", parent: str | None = None,
            layout: str = "cards") -> dict:
    # layout: "cards" (ring/value grid, drillable) or "list" (label:value detail)
    return {"title": title, "path": path, "parent": parent, "layout": layout, "rows": rows}


def _pct(used: float, total: float) -> int:
    if not total:
        return 0
    return max(0, min(100, round(used / total * 100)))


def _bytes(n) -> str:
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return f"{n:.0f}{u}" if u in ("B", "K", "M") else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


# --- PVE helpers ------------------------------------------------------------

async def _pve_get(client: httpx.AsyncClient, cfg: config.Config, sub: str):
    base = f"https://{cfg.pve_host}/api2/json"
    headers = {"Authorization": f"PVEAPIToken={cfg.pve_token_id}={cfg.pve_secret}"}
    r = await client.get(f"{base}/{sub}", headers=headers, timeout=cfg.http_timeout)
    r.raise_for_status()
    return r.json().get("data")


async def _pbs_get(client: httpx.AsyncClient, cfg: config.Config, sub: str):
    base = f"https://{cfg.pbs_host}/api2/json"
    headers = {"Authorization": f"PBSAPIToken={cfg.pbs_token_id}:{cfg.pbs_secret}"}  # colon, not =
    r = await client.get(f"{base}/{sub}", headers=headers, timeout=cfg.http_timeout)
    r.raise_for_status()
    return r.json().get("data")


# --- TrueNAS helper (JSON-RPC over WebSocket, mirrors app.fetch_truenas) -----

async def _tn_call(cfg: config.Config, method: str, params=None):
    uri = f"wss://{cfg.truenas_host}/api/current"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with asyncio.timeout(cfg.http_timeout * 2):
        async with websockets.connect(uri, ssl=ctx) as ws:
            rid = 0

            async def call(m, p=None):
                nonlocal rid
                rid += 1
                mid = rid
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "method": m, "params": p or []}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") != mid:
                        continue
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result")

            if not await call("auth.login_with_api_key", [cfg.truenas_key]):
                raise RuntimeError("auth failed")
            return await call(method, params)


# --- UniFi helper (Integration API, X-API-KEY) -------------------------------

async def _unifi_get(client: httpx.AsyncClient, cfg: config.Config, sub: str):
    base = f"https://{cfg.unifi_host}/proxy/network/integration/v1"
    headers = {"X-API-KEY": cfg.unifi_key, "Accept": "application/json"}
    r = await client.get(f"{base}/{sub}", headers=headers, timeout=cfg.http_timeout)
    r.raise_for_status()
    body = r.json()
    # Integration API wraps lists in {"data": [...]}; pass dicts through as-is.
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


async def _unifi_site_id(client: httpx.AsyncClient, cfg: config.Config) -> str:
    sites = await _unifi_get(client, cfg, "sites") or []
    if cfg.unifi_site and cfg.unifi_site != "default":
        for s in sites:
            if s.get("name") == cfg.unifi_site or s.get("id") == cfg.unifi_site:
                return s["id"]
    return sites[0]["id"] if sites else cfg.unifi_site


# --- entry point ------------------------------------------------------------

async def screen(path: str) -> dict:
    cfg = config.get()
    path = (path or "").strip("/")
    try:
        async with httpx.AsyncClient(verify=False) as client:
            if path == "":
                return _home()
            parts = path.split("/")
            if parts[0] == "pve":
                return await _pve(client, cfg, parts[1:], path)
            if parts[0] == "truenas":
                return await _truenas(cfg, parts[1:], path)
            if parts[0] == "pbs":
                return await _pbs(client, cfg, parts[1:], path)
            if parts[0] == "unifi":
                return await _unifi(client, cfg, parts[1:], path)
            # other providers land here later (plex)
            return _screen(parts[0].upper(), [_row("Not implemented yet", state="muted")],
                           path, parent="")
    except Exception as e:
        return _screen("Error", [_row(type(e).__name__, str(e)[:40], "crit")], path, parent="")


def _home() -> dict:
    return _screen("HOMELAB", [
        _row("Cluster", "PVE", "ok", "pve/cluster"),
        _row("Pools", "TrueNAS", "ok", "truenas/pools"),
        _row("Backups", "PBS", "ok", "pbs/datastores"),
        _row("Network", "UniFi", "ok", "unifi"),
    ], path="")


# --- PVE provider -----------------------------------------------------------

async def _pve(client, cfg, sub, path) -> dict:
    if not (cfg.pve_host and cfg.pve_token_id and cfg.pve_secret):
        return _screen("CLUSTER", [_row("PVE not configured", state="crit")], path, parent="")

    # cluster: list nodes
    if not sub or sub[0] == "cluster":
        nodes = await _pve_get(client, cfg, "cluster/resources?type=node") or []
        rows = []
        for n in sorted(nodes, key=lambda x: x.get("node", "")):
            up = n.get("status") == "online"
            cpu = round((n.get("cpu") or 0) * 100)
            mem = _pct(n.get("mem", 0), n.get("maxmem", 0))
            rows.append(_row(n.get("node", "?"),
                             n.get("status", "?"),
                             "ok" if up else "crit",
                             f"pve/node/{quote(str(n.get('node')), safe='')}",
                             cpu=cpu if up else 0, mem=mem if up else 0))
        return _screen("CLUSTER", rows, path, parent="")

    # node: its VMs/LXCs as gauge cards
    if sub[0] == "node" and len(sub) >= 2:
        node = sub[1]
        guests = await _pve_get(client, cfg, "cluster/resources?type=vm") or []
        here = sorted([g for g in guests if g.get("node") == node],
                      key=lambda x: x.get("vmid", 0))
        rows = []
        for g in here:
            running = g.get("status") == "running"
            kind = "vm" if g.get("type") == "qemu" else "ct"
            gcpu = round((g.get("cpu") or 0) * 100) if running else 0
            gmem = _pct(g.get("mem", 0), g.get("maxmem", 0)) if running else 0
            rows.append(_row(f"{kind} {g.get('vmid')} {g.get('name', '')}".strip(),
                             g.get("status", "?"),
                             "ok" if running else "muted",
                             f"pve/guest/{g.get('vmid')}",
                             cpu=gcpu, mem=gmem))
        if not rows:
            rows.append(_row("No guests", state="muted"))
        return _screen(node, rows, path, parent="pve/cluster")

    # guest: VM/LXC detail
    if sub[0] == "guest" and len(sub) >= 2:
        vmid = sub[1]
        guests = await _pve_get(client, cfg, "cluster/resources?type=vm") or []
        g = next((x for x in guests if str(x.get("vmid")) == str(vmid)), None)
        if not g:
            return _screen(f"vm {vmid}", [_row("Not found", state="crit")], path, parent="pve/cluster")
        running = g.get("status") == "running"
        cpu = round((g.get("cpu") or 0) * 100)
        memp = _pct(g.get("mem", 0), g.get("maxmem", 0))
        diskp = _pct(g.get("disk", 0), g.get("maxdisk", 0))
        rows = [
            _row("Type", "VM" if g.get("type") == "qemu" else "LXC", "muted"),
            _row("Status", g.get("status", "?"), "ok" if running else "muted"),
            _row("Node", g.get("node", "?"), "muted"),
            _row("CPU", f"{cpu}%", "warn" if cpu >= 85 else "ok"),
            _row("Memory", f"{memp}%  {_bytes(g.get('mem', 0))}/{_bytes(g.get('maxmem', 0))}",
                 "warn" if memp >= 85 else "ok"),
            _row("Disk", f"{diskp}%  {_bytes(g.get('disk', 0))}/{_bytes(g.get('maxdisk', 0))}",
                 "warn" if diskp >= 90 else "ok"),
            _row("Uptime", _dur(int(g.get("uptime", 0))), "muted"),
        ]
        name = f"{g.get('vmid')} {g.get('name', '')}".strip()
        return _screen(name, rows, path, parent=f"pve/node/{g.get('node')}", layout="list")

    return _screen("CLUSTER", [_row("Unknown path", state="crit")], path, parent="")


# --- TrueNAS provider -------------------------------------------------------

async def _truenas(cfg: config.Config, sub, path) -> dict:
    if not (cfg.truenas_host and cfg.truenas_key):
        return _screen("POOLS", [_row("TrueNAS not configured", state="crit")], path, parent="")
    pools = await _tn_call(cfg, "pool.query") or []

    # pools list -> single-ring cards (used %)
    if not sub or sub[0] == "pools":
        rows = []
        for p in sorted(pools, key=lambda x: x.get("name", "")):
            used = _pct(p.get("allocated", 0), p.get("size", 0))
            ok = bool(p.get("healthy")) and p.get("status") == "ONLINE"
            rows.append(_row(p.get("name", "?"), p.get("status", "?"),
                             "ok" if ok else "crit",
                             f"truenas/pool/{quote(str(p.get('name')), safe='')}",
                             cpu=used))
        return _screen("POOLS", rows, path, parent="")

    # pool detail -> stats + drive health + cleanup info
    if sub[0] == "pool" and len(sub) >= 2:
        name = sub[1]
        p = next((x for x in pools if x.get("name") == name), None)
        if not p:
            return _screen(name, [_row("Not found", state="crit")], path,
                           parent="truenas/pools", layout="list")
        used = _pct(p.get("allocated", 0), p.get("size", 0))
        ok = bool(p.get("healthy")) and p.get("status") == "ONLINE"

        online = total = 0
        vtypes = []
        for v in (p.get("topology") or {}).get("data", []):
            if v.get("type"):
                vtypes.append(v["type"])
            for d in v.get("children", []):
                total += 1
                if d.get("status") == "ONLINE":
                    online += 1

        scan = p.get("scan") or {}
        et = (scan.get("end_time") or {}).get("$date")
        if et:
            days = (time.time() - et / 1000) / 86400
            errs = scan.get("errors", 0)
            scrub = f"{days:.0f}d ago, {errs} err"
            scrub_state = "ok" if not errs else "crit"
        else:
            scrub, scrub_state = "never", "muted"

        rows = [
            _row("Status", p.get("status", "?"), "ok" if ok else "crit"),
            _row("Used", f"{_bytes(p.get('allocated', 0))} ({used}%)", "warn" if used >= 85 else "ok"),
            _row("Free", _bytes(p.get("free", 0)), "muted"),
            _row("Fragment", f"{p.get('fragmentation', 0)}%", "muted"),
            _row("Auto TRIM", (p.get("autotrim") or {}).get("value", "?"), "muted"),
            _row("Last Scrub", scrub, scrub_state),
            _row("Layout", ", ".join(vtypes) or "?", "muted"),
            _row("Disks", f"{online}/{total} ONLINE", "ok" if total and online == total else "crit"),
        ]
        return _screen(name, rows, path, parent="truenas/pools", layout="list")

    return _screen("POOLS", [_row("Unknown path", state="crit")], path, parent="")


# --- PBS provider -----------------------------------------------------------

async def _pbs(client, cfg: config.Config, sub, path) -> dict:
    if not (cfg.pbs_host and cfg.pbs_token_id and cfg.pbs_secret):
        return _screen("BACKUPS", [_row("PBS not configured", state="crit")], path, parent="")
    usage = await _pbs_get(client, cfg, "status/datastore-usage") or []

    # datastores list -> single-ring cards (used %)
    if not sub or sub[0] == "datastores":
        rows = []
        for d in sorted(usage, key=lambda x: x.get("store", "")):
            used = _pct(d.get("used", 0), d.get("total", 0))
            rows.append(_row(d.get("store", "?"), _bytes(d.get("used", 0)),
                             "warn" if used >= 85 else "ok",
                             f"pbs/datastore/{quote(str(d.get('store')), safe='')}",
                             cpu=used))
        return _screen("BACKUPS", rows, path, parent="")

    # datastore detail
    if sub[0] == "datastore" and len(sub) >= 2:
        name = sub[1]
        d = next((x for x in usage if x.get("store") == name), None)
        if not d:
            return _screen(name, [_row("Not found", state="crit")], path,
                           parent="pbs/datastores", layout="list")
        total = d.get("total", 0)
        used = d.get("used", 0)
        usedp = _pct(used, total)
        avail = d.get("avail", total - used)
        rows = [
            _row("Used", f"{_bytes(used)} ({usedp}%)", "warn" if usedp >= 85 else "ok"),
            _row("Total", _bytes(total), "muted"),
            _row("Free", _bytes(avail), "muted"),
        ]
        q = quote(str(name), safe="")
        # last GC (best-effort; needs Sys.Audit which the Audit role grants)
        try:
            tasks = await _pbs_get(
                client, cfg,
                f"nodes/{cfg.pbs_node}/tasks?store={q}&typefilter=garbage_collection&limit=1") or []
            if tasks and tasks[0].get("endtime"):
                days = (time.time() - tasks[0]["endtime"]) / 86400
                ok = (tasks[0].get("status") or "OK") == "OK"
                rows.append(_row("Last GC", f"{days:.0f}d ago", "ok" if ok else "warn"))
        except Exception:
            pass
        # last verify (best-effort)
        try:
            tasks = await _pbs_get(
                client, cfg,
                f"nodes/{cfg.pbs_node}/tasks?store={q}&typefilter=verify&limit=1") or []
            if tasks and tasks[0].get("endtime"):
                days = (time.time() - tasks[0]["endtime"]) / 86400
                ok = (tasks[0].get("status") or "OK") == "OK"
                rows.append(_row("Last Verify", f"{days:.0f}d ago", "ok" if ok else "warn"))
        except Exception:
            pass
        # backup group count (best-effort)
        try:
            groups = await _pbs_get(client, cfg, f"admin/datastore/{q}/groups") or []
            rows.append(_row("Backup Groups", str(len(groups)), "muted"))
        except Exception:
            pass
        return _screen(name, rows, path, parent="pbs/datastores", layout="list")

    return _screen("BACKUPS", [_row("Unknown path", state="crit")], path, parent="")


# --- UniFi provider ---------------------------------------------------------

_GW_KEYWORDS = ("dream machine", "gateway", "uxg", "ucg", "udr", "udw", "udm")


def _mbps(bps) -> str:
    m = float(bps or 0) * 8 / 1_000_000  # bytes/s -> Mbit/s
    return f"{m:.1f} Mbps" if m < 1000 else f"{m/1000:.2f} Gbps"


async def _unifi(client, cfg: config.Config, sub, path) -> dict:
    if not (cfg.unifi_host and cfg.unifi_key):
        return _screen("NETWORK", [_row("UniFi not configured", state="crit")],
                       path, parent="", layout="list")
    site = await _unifi_site_id(client, cfg)
    devices = await _unifi_get(client, cfg, f"sites/{site}/devices") or []

    # root: gateway load + WAN throughput + client/device counts
    if not sub or sub[0] == "root":
        gw = next((d for d in devices
                   if any(k in (d.get("model", "") + d.get("name", "")).lower()
                          for k in _GW_KEYWORDS)), devices[0] if devices else None)
        st = {}
        if gw:
            try:
                st = await _unifi_get(client, cfg,
                                      f"sites/{site}/devices/{gw['id']}/statistics/latest") or {}
            except Exception:
                pass
        up = (st.get("uplink") or {})
        clients = await _unifi_get(client, cfg, f"sites/{site}/clients") or []
        wired = sum(1 for c in clients if c.get("type") == "WIRED")
        online = sum(1 for d in devices if d.get("state") == "ONLINE")
        cpu = st.get("cpuUtilizationPct")
        mem = st.get("memoryUtilizationPct")
        rows = [
            _row("WAN Down", _mbps(up.get("rxRateBps")), "ok"),
            _row("WAN Up", _mbps(up.get("txRateBps")), "ok"),
            _row("GW CPU", f"{cpu:.0f}%" if cpu is not None else "?",
                 "warn" if (cpu or 0) >= 85 else "ok"),
            _row("GW Mem", f"{mem:.0f}%" if mem is not None else "?",
                 "warn" if (mem or 0) >= 85 else "ok"),
            _row("Load", f"{st.get('loadAverage1Min', 0):.2f}", "muted"),
            _row("Uptime", _dur(int(st.get("uptimeSec", 0))), "muted"),
            _row("Clients", f"{len(clients)} ({wired}w / {len(clients)-wired}wl)", "muted"),
            _row("Devices", f"{online}/{len(devices)} online",
                 "ok" if online == len(devices) else "warn"),
        ]
        return _screen("NETWORK", rows, path, parent="", layout="list")

    return _screen("NETWORK", [_row("Unknown path", state="crit")], path, parent="")


def _dur(sec: int) -> str:
    if sec <= 0:
        return "-"
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
