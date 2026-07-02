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

import time
from urllib.parse import quote

import backends
import config
from backends import api_get, duration as _dur, format_bytes as _bytes, pct as _pct


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


async def _pve_get(cfg: config.Config, sub: str):
    return await api_get(f"{backends.pve_base(cfg.pve_host)}/{sub}",
                         backends.pve_headers(cfg.pve_token_id, cfg.pve_secret),
                         cfg.http_timeout)


async def _pbs_get(cfg: config.Config, sub: str):
    return await api_get(f"{backends.pbs_base(cfg.pbs_host)}/{sub}",
                         backends.pbs_headers(cfg.pbs_token_id, cfg.pbs_secret),
                         cfg.http_timeout)


async def _unifi_get(cfg: config.Config, sub: str):
    return await api_get(f"{backends.unifi_base(cfg.unifi_host)}/{sub}",
                         backends.unifi_headers(cfg.unifi_key),
                         cfg.http_timeout)


# --- entry point ------------------------------------------------------------

async def screen(path: str) -> dict:
    cfg = config.get()
    path = (path or "").strip("/")
    try:
        if path == "":
            return _home()
        parts = path.split("/")
        if parts[0] == "pve":
            return await _pve(cfg, parts[1:], path)
        if parts[0] == "truenas":
            return await _truenas(cfg, parts[1:], path)
        if parts[0] == "pbs":
            return await _pbs(cfg, parts[1:], path)
        if parts[0] == "unifi":
            return await _unifi(cfg, parts[1:], path)
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

async def _pve(cfg, sub, path) -> dict:
    if not (cfg.pve_host and cfg.pve_token_id and cfg.pve_secret):
        return _screen("CLUSTER", [_row("PVE not configured", state="crit")], path, parent="")

    # cluster: list nodes
    if not sub or sub[0] == "cluster":
        nodes = await _pve_get(cfg, "cluster/resources?type=node") or []
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
        guests = await _pve_get(cfg, "cluster/resources?type=vm") or []
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
        guests = await _pve_get(cfg, "cluster/resources?type=vm") or []
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
    async with backends.truenas_session(
        cfg.truenas_host, cfg.truenas_key, cfg.http_timeout
    ) as rpc:
        pools = await rpc.call("pool.query") or []

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

async def _pbs(cfg: config.Config, sub, path) -> dict:
    if not (cfg.pbs_host and cfg.pbs_token_id and cfg.pbs_secret):
        return _screen("BACKUPS", [_row("PBS not configured", state="crit")], path, parent="")
    usage = await _pbs_get(cfg, "status/datastore-usage") or []

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
                cfg, f"nodes/{cfg.pbs_node}/tasks?store={q}&typefilter=garbage_collection&limit=1") or []
            if tasks and tasks[0].get("endtime"):
                days = (time.time() - tasks[0]["endtime"]) / 86400
                ok = (tasks[0].get("status") or "OK") == "OK"
                rows.append(_row("Last GC", f"{days:.0f}d ago", "ok" if ok else "warn"))
        except Exception:
            pass
        # last verify (best-effort)
        try:
            tasks = await _pbs_get(
                cfg, f"nodes/{cfg.pbs_node}/tasks?store={q}&typefilter=verify&limit=1") or []
            if tasks and tasks[0].get("endtime"):
                days = (time.time() - tasks[0]["endtime"]) / 86400
                ok = (tasks[0].get("status") or "OK") == "OK"
                rows.append(_row("Last Verify", f"{days:.0f}d ago", "ok" if ok else "warn"))
        except Exception:
            pass
        # backup group count (best-effort)
        try:
            groups = await _pbs_get(cfg, f"admin/datastore/{q}/groups") or []
            rows.append(_row("Backup Groups", str(len(groups)), "muted"))
        except Exception:
            pass
        return _screen(name, rows, path, parent="pbs/datastores", layout="list")

    return _screen("BACKUPS", [_row("Unknown path", state="crit")], path, parent="")


# --- UniFi provider ---------------------------------------------------------

def _mbps(bps) -> str:
    m = float(bps or 0) * 8 / 1_000_000  # bytes/s -> Mbit/s
    return f"{m:.1f} Mbps" if m < 1000 else f"{m/1000:.2f} Gbps"


async def _unifi(cfg: config.Config, sub, path) -> dict:
    if not (cfg.unifi_host and cfg.unifi_key):
        return _screen("NETWORK", [_row("UniFi not configured", state="crit")],
                       path, parent="", layout="list")
    site = await backends.unifi_site_id(
        cfg.unifi_host, cfg.unifi_key, cfg.unifi_site, cfg.http_timeout)
    devices = await _unifi_get(cfg, f"sites/{site}/devices") or []

    # root: gateway load + WAN throughput + client/device counts
    if not sub or sub[0] == "root":
        gw = backends.pick_gateway(devices)
        st = {}
        if gw:
            try:
                st = await _unifi_get(cfg, f"sites/{site}/devices/{gw['id']}/statistics/latest") or {}
            except Exception:
                pass
        up = (st.get("uplink") or {})
        clients = await _unifi_get(cfg, f"sites/{site}/clients") or []
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
