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

import httpx

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
            # other providers land here later (truenas, pbs, unifi, plex)
            return _screen(parts[0].upper(), [_row("Not implemented yet", state="muted")],
                           path, parent="")
    except Exception as e:
        return _screen("Error", [_row(type(e).__name__, str(e)[:40], "crit")], path, parent="")


def _home() -> dict:
    return _screen("HOMELAB", [
        _row("Cluster", "PVE", "ok", "pve/cluster"),
        _row("Pools", "TrueNAS", "muted", "truenas/pools"),
        _row("Backups", "PBS", "muted", "pbs/datastores"),
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
                             f"pve/node/{n.get('node')}",
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
