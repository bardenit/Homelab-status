"""
Runtime configuration for the aggregator.

Precedence: the JSON file written by the admin UI (CONFIG_PATH, default
/data/config.json) overrides environment variables, which override the
built-in defaults. Env vars seed the initial values on first run; once the
JSON file exists it is the source of truth and the admin UI owns it.

This replaces the old "config is env vars only" model: the admin UI needs a
writable place to persist hosts, tokens, and the admin password hash.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger("aggregator.config")

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/data/config.json"))

# Set when CONFIG_PATH exists but could not be read or parsed (bad volume
# permissions, corrupt JSON). Surfaced via /healthz so a broken volume shows
# up as a config error instead of a mystery "Incorrect password" at login.
load_error: "str | None" = None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _envbool(key: str) -> bool:
    return _env(key).lower() in ("1", "true", "yes")


def _split_token(combined: str, sep: str) -> "tuple[str, str]":
    """Split a combined 'user@realm!id<sep>secret' into (id, secret)."""
    combined = (combined or "").strip()
    if sep in combined:
        left, right = combined.split(sep, 1)
        return left.strip(), right.strip()
    return combined, ""


@dataclass
class Config:
    # mode
    mock: bool = False
    # Proxmox VE (id like 'jason@pam!LabMonitor' joined to secret with '=')
    pve_host: str = ""
    pve_token_id: str = ""
    pve_secret: str = ""
    # TrueNAS (a single raw API key, no separate id)
    truenas_host: str = ""
    truenas_key: str = ""
    # PBS (id joined to secret with a COLON, unlike PVE's '=')
    pbs_host: str = ""
    pbs_token_id: str = ""
    pbs_secret: str = ""
    pbs_node: str = "localhost"
    # UniFi (UDM/UniFi OS: Integration API key, X-API-KEY header; site "default")
    unifi_host: str = ""
    unifi_key: str = ""
    unifi_site: str = "default"
    # alert thresholds (percent)
    mem_warn: float = 90
    pool_warn: float = 85
    pbs_warn: float = 85
    # tuning
    cache_ttl: float = 10
    http_timeout: float = 6
    # firmware OTA: min seconds between manifest fetches per panel IP
    fw_min_interval: float = 30
    # auth / session (never rendered in the config form)
    admin_password_hash: str = ""
    session_secret: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        pve_id, pve_secret = _split_token(_env("PVE_TOKEN"), "=")
        pbs_id, pbs_secret = _split_token(_env("PBS_TOKEN"), ":")
        return cls(
            mock=_envbool("MOCK"),
            pve_host=_env("PVE_HOST"),
            pve_token_id=pve_id,
            pve_secret=pve_secret,
            truenas_host=_env("TRUENAS_HOST"),
            truenas_key=_env("TRUENAS_KEY"),
            pbs_host=_env("PBS_HOST"),
            pbs_token_id=pbs_id,
            pbs_secret=pbs_secret,
            pbs_node=_env("PBS_NODE", "localhost"),
            unifi_host=_env("UNIFI_HOST"),
            unifi_key=_env("UNIFI_KEY"),
            unifi_site=_env("UNIFI_SITE", "default"),
            mem_warn=_envf("MEM_WARN", 90),
            pool_warn=_envf("POOL_WARN", 85),
            pbs_warn=_envf("PBS_WARN", 85),
            cache_ttl=_envf("CACHE_TTL", 10),
            http_timeout=_envf("HTTP_TIMEOUT", 6),
            fw_min_interval=_envf("FW_MIN_INTERVAL", 30),
        )


# --- password hashing (stdlib PBKDF2, no external dep) ----------------------

def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# --- load / save singleton --------------------------------------------------

_cfg: "Config | None" = None


def load() -> Config:
    """Build the live config from defaults + env, then overlay the JSON file."""
    global _cfg, load_error
    cfg = Config.from_env()
    load_error = None
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            known = {f.name for f in fields(Config)}
            for key, value in data.items():
                if key in known:
                    setattr(cfg, key, value)
            # migrate legacy combined tokens (pre-split schema)
            if data.get("pve_token"):
                cfg.pve_token_id, cfg.pve_secret = _split_token(data["pve_token"], "=")
            if data.get("pbs_token"):
                cfg.pbs_token_id, cfg.pbs_secret = _split_token(data["pbs_token"], ":")
        except Exception as e:
            # Do NOT silently fall back: this is how a volume permission problem
            # once masqueraded as a login failure. Run on env/defaults so the
            # service stays up, but shout, flag it, and never write over the
            # file we could not read.
            load_error = f"{type(e).__name__}: {e}"
            log.error(
                "FAILED to read %s (%s); running on env/defaults. "
                "Check the volume ownership (app runs as uid 1000) and JSON validity.",
                CONFIG_PATH, load_error,
            )
    # a stable session secret so cookies survive restarts; persist best-effort,
    # but never touch the config file if we failed to read it above
    if not cfg.session_secret:
        cfg.session_secret = secrets.token_urlsafe(32)
        if load_error is None:
            try:
                _write(cfg)
            except Exception:
                pass
    _cfg = cfg
    return _cfg


def get() -> Config:
    return _cfg if _cfg is not None else load()


def _write(cfg: Config) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w", dir=str(CONFIG_PATH.parent), delete=False
    )
    try:
        json.dump(asdict(cfg), tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, CONFIG_PATH)  # atomic
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def save(cfg: Config) -> None:
    global _cfg
    _write(cfg)
    _cfg = cfg
