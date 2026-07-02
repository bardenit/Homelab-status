"""
Mock-mode smoke tests: payload contract, admin gating, config behavior.

These run with MOCK=1 and a temp CONFIG_PATH, so no backends are needed.
The payload-shape assertions are the contract the CYD firmware parses by
field name; if one fails, the parse lambda in homelab-panel.yaml is at risk.
"""

import os
import tempfile
from pathlib import Path

_tmpdir = tempfile.mkdtemp(prefix="lab-health-test-")
os.environ["MOCK"] = "1"
os.environ["CONFIG_PATH"] = str(Path(_tmpdir) / "config.json")

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import config  # noqa: E402

client = TestClient(app_module.app)


# --- payload contracts (what the firmware parses) ----------------------------

def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mock"] is True
    assert body["config"] == "ok"


def test_status_contract():
    r = client.get("/api/status")
    assert r.status_code == 200
    p = r.json()
    assert set(p) == {"ts", "quorate", "nodes", "pools", "pbs", "sources", "alert"}
    for n in p["nodes"]:
        assert set(n) >= {"name", "up", "cpu", "mem"}
        assert isinstance(n["cpu"], int) and isinstance(n["mem"], int)
    for pool in p["pools"]:
        assert set(pool) >= {"name", "health", "ok", "used"}
        assert isinstance(pool["used"], int)
    for d in p["pbs"]:
        assert set(d) >= {"name", "used", "gc_age_h"}
    assert set(p["sources"]) == {"pve", "truenas", "pbs"}
    assert isinstance(p["alert"], bool)


def test_unifi_contract():
    r = client.get("/api/unifi")
    assert r.status_code == 200
    u = r.json()
    assert set(u) == {"wan_down", "wan_up", "cpu", "mem",
                      "clients", "dev_online", "dev_total", "err"}


def test_ui_screen_shape():
    r = client.get("/ui")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {"title", "path", "parent", "layout", "rows"}
    assert s["layout"] in ("cards", "list")
    for row in s["rows"]:
        assert set(row) >= {"label", "value", "state", "drill"}
        assert row["state"] in ("ok", "warn", "crit", "muted")


def test_ui_unconfigured_source_does_not_500():
    r = client.get("/ui/pve/cluster")
    assert r.status_code == 200
    assert r.json()["rows"][0]["label"] == "PVE not configured"


# --- admin gating and headers -------------------------------------------------

def test_admin_redirects_to_setup_when_no_password():
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/setup"
    # POST login must not dead-end at "Incorrect password" in this state
    r = client.post("/admin/login", data={"password": "x"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/setup"


def test_security_headers_on_admin():
    r = client.get("/admin", follow_redirects=False)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_setup_login_and_secret_not_echoed():
    # first-run setup logs us in
    r = client.post("/admin/setup",
                    data={"password": "hunter2hunter2", "confirm": "hunter2hunter2"},
                    follow_redirects=False)
    assert r.status_code == 303

    # plant a secret, then confirm the config form never echoes it
    cfg = config.get()
    cfg.pve_secret = "SUPER-SECRET-UUID"
    config.save(cfg)
    r = client.get("/admin")
    assert r.status_code == 200
    assert "SUPER-SECRET-UUID" not in r.text
    assert "leave blank to keep" in r.text

    # saving the form with a blank secret keeps the stored value
    # ("mock" kept on so later tests still run backend-free)
    r = client.post("/admin/config", data={"pve_secret": "", "mock": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert config.get().pve_secret == "SUPER-SECRET-UUID"

    # wrong password is rejected once a hash exists
    fresh = TestClient(app_module.app)
    r = fresh.post("/admin/login", data={"password": "wrong"})
    assert r.status_code == 401

    # tests (probe) endpoints refuse unauthenticated callers
    r = fresh.post("/admin/test/pve", data={})
    assert r.status_code == 401


def test_saved_secret_only_used_against_saved_host():
    # runs logged-in (setup happened above); the saved secret must not follow
    # a Test request pointed at a different host
    cfg = config.get()
    cfg.pve_host = "127.0.0.1:1"  # nothing listens here: connect fails fast
    cfg.pve_secret = "SUPER-SECRET-UUID"
    config.save(cfg)

    # different host + blank secret: no fallback, probe reports the missing field
    r = client.post("/admin/test/pve",
                    data={"pve_host": "evil.example", "pve_token_id": "x@pam!t",
                          "pve_secret": ""})
    assert "Fill in secret" in r.json()["detail"]

    # same host + blank secret: fallback applies, probe proceeds to connect
    r = client.post("/admin/test/pve",
                    data={"pve_host": "127.0.0.1:1", "pve_token_id": "x@pam!t",
                          "pve_secret": ""})
    assert "Could not connect" in r.json()["detail"]


def test_firmware_upload_size_cap():
    big = b"\xe9" + b"\x00" * (9 * 1024 * 1024)
    r = client.post("/admin/firmware",
                    data={"version": "x"},
                    files={"file": ("firmware.ota.bin", big)})
    assert r.status_code == 413
    assert "too large" in r.text


# --- config unit checks ---------------------------------------------------------

def test_password_hash_roundtrip():
    h = config.hash_password("correct horse")
    assert config.verify_password("correct horse", h)
    assert not config.verify_password("wrong", h)
    assert not config.verify_password("anything", "")


def test_config_json_overlays_env():
    cfg = config.get()
    cfg.pbs_node = "pbs-real-node"
    config.save(cfg)
    reloaded = config.load()
    assert reloaded.pbs_node == "pbs-real-node"
    assert config.load_error is None


def test_corrupt_config_sets_load_error_and_is_not_overwritten():
    path = Path(os.environ["CONFIG_PATH"])
    good = path.read_bytes()
    try:
        path.write_text("{not json")
        cfg = config.load()
        assert config.load_error is not None
        assert cfg.admin_password_hash == ""  # fell back to env/defaults
        # healthz surfaces it
        assert client.get("/healthz").json()["config"] == "error"
        # the broken file must not have been clobbered by a session-secret write
        assert path.read_text() == "{not json"
        # and first-run setup must refuse to overwrite it
        r = client.post("/admin/setup",
                        data={"password": "newpass123", "confirm": "newpass123"},
                        follow_redirects=False)
        assert r.status_code == 500
        assert path.read_text() == "{not json"
    finally:
        path.write_bytes(good)
        config.load()
