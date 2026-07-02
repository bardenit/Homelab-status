# HANDOFF: cleanup + hardening pass ready to test

Status: COMPLETE (2026-07-02). Deployed to the LXC, server-side checks all
passed live (healthz config:ok, all sources green, drill-downs, UniFi lane,
admin gating), and Jason confirmed admin login plus all four Test buttons
green. Nothing pending. No firmware change: the CYDs did not need a re-flash.

## Deploy (on the LXC at 10.10.10.14)

```bash
docker-compose pull && docker-compose up -d
```

If you have not already done it after the previous hardening deploy, the data
volume must be owned by uid 1000 (this was the cause of the "Incorrect
password" issue):

```bash
chown -R 1000:1000 ./data
```

## Quick health check

```bash
curl http://10.10.10.14/healthz
```

New field: `"config": "ok"` means /data/config.json loaded fine. If it says
`"error"`, the volume perms or the JSON are broken, and the container logs
(`docker-compose logs`) now say exactly why. This replaces the old silent
fallback that turned a perms problem into a login mystery.

## What to test

1. Both panels: glance pages (CLUSTER, POOLS, BACKUPS, NETWORK sparklines),
   drill-down taps, 1s WAN updates. Nothing should look different, it should
   just work (the UniFi lane now reuses one TLS connection instead of
   handshaking every second).
2. Admin UI at http://10.10.10.14/admin: log in, confirm the config form shows
   your hosts with blank secret fields ("saved, leave blank to keep"), press
   the four Test buttons, save once, and confirm sources stay green.
3. Firmware page still lists the panels and their versions.

## What changed (summary)

- New `backends.py`: one copy of the TrueNAS WebSocket RPC (was three), one
  copy of the PVE/PBS/UniFi helpers, one pooled HTTP client.
- `config.load()` fails loudly, never overwrites an unreadable config, and
  `/admin/setup` refuses to run when the saved config exists but is unreadable.
- Security fixes from the audit: Test buttons only use a saved secret against
  the saved host (no exfiltration to a typed-in host), firmware uploads capped
  at 8 MB, login attempts serialized (parallel brute-force cannot multiply
  CPU cost).
- CSP header on /admin, login/setup dead-ends fixed.
- 13-test smoke suite in `tests/`; mock payloads verified byte-identical to the
  previous image, so the panel contract is untouched.

Full details: docs/handoff.md (top entry) and the post-mortem in the session.
