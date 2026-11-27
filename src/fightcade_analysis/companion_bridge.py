"""Browser ↔ Local Companion bridge, pairing tokens, PNA/CORS security & remote stewardship config.

Implements Track 3 Companion bridge guarantees:
  - Strict origin allowlist + Chrome Private Network Access (`Access-Control-Allow-Private-Network: true`).
  - Per-install pairing token (`data/companion_pairing.json`) + short-lived 1-click pairing codes.
  - Packaged Companion build flag (`FC_PACKAGED_COMPANION=1` / frozen binary) strictly locks `pro_corpus_enabled = False`.
  - Remote stewardship config fetch (`https://fightcadelab.com/companion/config.json`) with safe offline fallback.
  - Optional hosted Fightcade metadata cache lookup (`/api/fightcade-meta/<quark_id>`) before direct Fightcade HEAD.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from fightcade_analysis.env_config import DEFAULT_DATA_DIR, validate_stream_target
from fightcade_analysis.fetch_replays import DEFAULT_UA

DEFAULT_CLOUD_BASE_URL = "https://fightcadelab.com"

ALLOWED_CROSS_ORIGINS: frozenset[str] = frozenset(
    {
        "https://fightcadelab.com",
        "https://www.fightcadelab.com",
    }
)

DEFAULT_REMOTE_COMPANION_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "fightcade_api_enabled": True,
    "min_refresh_cooldown_s": 60,
    "min_stream_cooldown_s": 10,
    "max_stream_cooldown_s": 15,
    "max_backfill_per_hour": 12,
    "community_worker_pool_enabled": False,
    "pro_corpus_enabled": False,
    "min_companion_version": "0.1.0",
    "source": "default",
}

_PAIRING_LOCK = threading.Lock()
_PENDING_PAIR_CODES: dict[str, dict[str, Any]] = {}
_PAIR_CODE_TTL_S = 300.0


def is_packaged_companion() -> bool:
    """Return True when running as a packaged/frozen public Companion build.

    Packaged Companions MUST always seal off Pro-Corpus features (`pro_corpus_enabled = False`).
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return True
    raw = str(os.environ.get("FC_PACKAGED_COMPANION") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def get_cloud_base_url() -> str:
    raw = str(os.environ.get("FC_CLOUD_BASE_URL") or DEFAULT_CLOUD_BASE_URL).strip()
    return raw.rstrip("/") or DEFAULT_CLOUD_BASE_URL


def is_allowed_companion_origin(origin: str | None, host_header: str | None = None) -> bool:
    """Check whether `Origin` is same-origin OR an explicitly allowlisted Fightcade Lab web origin."""
    if not origin:
        return True
    clean = origin.strip().rstrip("/")
    parsed = urlsplit(clean)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    # Same-origin local check (exact Host match only — no cross-port localhost wildcard)
    if host_header and parsed.scheme == "http" and parsed.netloc.lower() == host_header.strip().lower():
        return True

    # Explicit production hosted origins, configured FC_CLOUD_BASE_URL origin & optional FC_EXTRA_ALLOWED_ORIGINS
    allowed = set(ALLOWED_CROSS_ORIGINS)
    cloud_parsed = urlsplit(get_cloud_base_url())
    if cloud_parsed.scheme in ("http", "https") and cloud_parsed.netloc:
        allowed.add(f"{cloud_parsed.scheme}://{cloud_parsed.netloc.lower()}")
    extra = os.environ.get("FC_EXTRA_ALLOWED_ORIGINS") or ""
    for item in extra.split(","):
        u = item.strip().rstrip("/")
        if u:
            allowed.add(u)
    return f"{parsed.scheme}://{parsed.netloc.lower()}" in allowed


def is_cross_origin_allowed_web(origin: str | None, host_header: str | None = None) -> bool:
    """Return True when `Origin` is a non-same-origin allowlisted Fightcade Lab web origin."""
    if not origin:
        return False
    clean = origin.strip().rstrip("/")
    parsed = urlsplit(clean)
    if host_header and parsed.netloc.lower() == host_header.strip().lower():
        return False
    return is_allowed_companion_origin(origin, host_header)


def build_pna_cors_headers(
    origin: str | None,
    *,
    host_header: str | None = None,
    request_private_network: bool = False,
) -> dict[str, str]:
    """Build strict CORS + Chrome Private Network Access headers for allowlisted origins."""
    if not origin or not is_allowed_companion_origin(origin, host_header):
        return {}
    clean = origin.strip().rstrip("/")
    hdrs = {
        "Access-Control-Allow-Origin": clean,
        "Vary": "Origin, Access-Control-Request-Private-Network",
        "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Fightcade-Lab-Token",
        "Access-Control-Max-Age": "600",
    }
    if request_private_network or clean.startswith("https://"):
        hdrs["Access-Control-Allow-Private-Network"] = "true"
    return hdrs


# ---------------------------------------------------------------------------
# Pairing Token Store (`data/companion_pairing.json`)
# ---------------------------------------------------------------------------

def pairing_file_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "companion_pairing.json"


def _load_pairing_store(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    path = pairing_file_path(data_dir)
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("install_id") and doc.get("local_token"):
                return doc
        except Exception:  # noqa: BLE001
            pass
    doc = {
        "schema_version": 1,
        "install_id": f"fc_{secrets.token_hex(8)}",
        "local_token": f"fcl_{secrets.token_urlsafe(24)}",
        "paired_origins": [],
        "cloud_token": None,
        "paired_at": None,
    }
    _save_pairing_store(doc, data_dir)
    return doc


def _save_pairing_store(doc: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> None:
    path = pairing_file_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp_{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_cloud_token(data_dir: Path = DEFAULT_DATA_DIR) -> str | None:
    """Return the stored cloud Companion Bearer token (server-side only — never expose in HTTP JSON)."""
    with _PAIRING_LOCK:
        doc = _load_pairing_store(data_dir)
    raw = str(doc.get("cloud_token") or "").strip()
    return raw or None


def get_pairing_state(data_dir: Path = DEFAULT_DATA_DIR, *, include_token: bool = False) -> dict[str, Any]:
    with _PAIRING_LOCK:
        doc = _load_pairing_store(data_dir)
    has_cloud = bool(str(doc.get("cloud_token") or "").strip())
    out = {
        "install_id": doc["install_id"],
        "paired": bool(doc.get("paired_origins") or has_cloud),
        "has_cloud_token": has_cloud,
        "cloud_base_url": get_cloud_base_url(),
        "paired_origins": list(doc.get("paired_origins") or []),
        "paired_at": doc.get("paired_at"),
    }
    if include_token:
        out["local_token"] = doc["local_token"]
    return out


def verify_pairing_token(token: str | None, data_dir: Path = DEFAULT_DATA_DIR) -> bool:
    """Constant-time verification of a browser/CLI pairing token."""
    if not token or not isinstance(token, str):
        return False
    clean = token.strip()
    if not clean:
        return False
    with _PAIRING_LOCK:
        doc = _load_pairing_store(data_dir)
        expected = str(doc.get("local_token") or "")
    return bool(expected) and hmac.compare_digest(clean, expected)


def extract_bearer_or_header_token(headers: Any) -> str | None:
    tok = headers.get("X-Fightcade-Lab-Token")
    if tok and str(tok).strip():
        return str(tok).strip()
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        val = auth[7:].strip()
        if val:
            return val
    return None


def issue_pairing_code(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Generate a short-lived one-time pairing code (5 min TTL) for 1-click browser pairing."""
    now = time.monotonic()
    with _PAIRING_LOCK:
        doc = _load_pairing_store(data_dir)
        # Purge expired codes
        for k in [k for k, v in _PENDING_PAIR_CODES.items() if now > v["expires_at"]]:
            _PENDING_PAIR_CODES.pop(k, None)
        code = secrets.token_urlsafe(12)
        _PENDING_PAIR_CODES[code] = {
            "expires_at": now + _PAIR_CODE_TTL_S,
            "install_id": doc["install_id"],
        }
    pair_url = f"{get_cloud_base_url()}/pair?code={code}"
    return {
        "ok": True,
        "code": code,
        "pair_url": pair_url,
        "expires_in_s": int(_PAIR_CODE_TTL_S),
        "install_id": doc["install_id"],
    }


def complete_pairing(
    code: str,
    *,
    origin: str | None = None,
    host_header: str | None = None,
    cloud_token: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Exchange a valid one-time pairing code for the local Companion pairing token."""
    clean_code = str(code or "").strip()
    if not clean_code:
        raise ValueError("Pairing code is required")
    clean_origin: str | None = None
    if origin is not None and str(origin).strip():
        clean_origin = str(origin).strip().rstrip("/")
        if not is_allowed_companion_origin(clean_origin, host_header):
            raise ValueError(f"Untrusted pairing origin: {clean_origin}")
    now = time.monotonic()
    with _PAIRING_LOCK:
        rec = _PENDING_PAIR_CODES.pop(clean_code, None)
        if rec is None or now > float(rec["expires_at"]):
            raise ValueError("Invalid or expired pairing code")
        doc = _load_pairing_store(data_dir)
        origins = list(doc.get("paired_origins") or [])
        if clean_origin:
            if clean_origin not in origins:
                origins.append(clean_origin)
        doc["paired_origins"] = origins
        if cloud_token and str(cloud_token).strip():
            doc["cloud_token"] = str(cloud_token).strip()
        doc["paired_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_pairing_store(doc, data_dir)
        return {
            "ok": True,
            "install_id": doc["install_id"],
            "token": doc["local_token"],
            "paired_origins": origins,
            "paired_at": doc["paired_at"],
        }


def revoke_pairing(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Rotate the local Companion token and clear paired browser origins."""
    with _PAIRING_LOCK:
        doc = _load_pairing_store(data_dir)
        doc["local_token"] = f"fcl_{secrets.token_urlsafe(24)}"
        doc["paired_origins"] = []
        doc["cloud_token"] = None
        doc["paired_at"] = None
        _PENDING_PAIR_CODES.clear()
        _save_pairing_store(doc, data_dir)
        return {"ok": True, "install_id": doc["install_id"], "paired": False}


# ---------------------------------------------------------------------------
# Remote Stewardship Config (`/companion/config.json`) & Meta Cache
# ---------------------------------------------------------------------------

def remote_config_cache_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "remote_config_cache.json"


def _sanitize_remote_config(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(DEFAULT_REMOTE_COMPANION_CONFIG)
    if "fightcade_api_enabled" in raw:
        cfg["fightcade_api_enabled"] = bool(raw["fightcade_api_enabled"])
    for key, lo, hi in (
        ("min_refresh_cooldown_s", 15, 3600),
        ("min_stream_cooldown_s", 5, 300),
        ("max_stream_cooldown_s", 5, 600),
        ("max_backfill_per_hour", 0, 60),
    ):
        if key in raw:
            try:
                val = int(raw[key])
                cfg[key] = max(lo, min(hi, val))
            except (TypeError, ValueError):
                pass
    if cfg["max_stream_cooldown_s"] < cfg["min_stream_cooldown_s"]:
        cfg["max_stream_cooldown_s"] = cfg["min_stream_cooldown_s"]
    cfg["community_worker_pool_enabled"] = bool(raw.get("community_worker_pool_enabled", False))
    cfg["pro_corpus_enabled"] = False  # Remote config can NEVER force Pro-Corpus on
    if isinstance(raw.get("min_companion_version"), str):
        cfg["min_companion_version"] = raw["min_companion_version"][:32]
    return cfg


def load_cached_remote_config(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    p = remote_config_cache_path(data_dir)
    if p.is_file():
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                sanitized = _sanitize_remote_config(doc)
                sanitized["source"] = "cache"
                return sanitized
        except Exception:  # noqa: BLE001
            pass
    return dict(DEFAULT_REMOTE_COMPANION_CONFIG)


def fetch_remote_companion_config(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    base_url: str | None = None,
    timeout_s: float = 5.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch remote `/companion/config.json` with graceful fallback to cache/defaults."""
    url = f"{(base_url or get_cloud_base_url()).rstrip('/')}/companion/config.json"
    sess = session or requests.Session()
    try:
        resp = sess.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout_s)
        if resp.status_code == 200:
            raw = resp.json()
            if isinstance(raw, dict):
                cfg = _sanitize_remote_config(raw)
                cfg["source"] = "remote"
                cfg["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                p = remote_config_cache_path(data_dir)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                return cfg
    except Exception:  # noqa: BLE001
        pass
    return load_cached_remote_config(data_dir)


def lookup_remote_fightcade_meta(
    quark_id: str,
    *,
    base_url: str | None = None,
    timeout_s: float = 4.0,
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    """Query hosted `/api/fightcade-meta/<quark_id>` cache (avoids redundant Fightcade HEAD calls)."""
    qid = str(quark_id or "").strip()
    if not qid:
        return None
    url = f"{(base_url or get_cloud_base_url()).rstrip('/')}/api/fightcade-meta/{qid}"
    sess = session or requests.Session()
    try:
        resp = sess.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout_s)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("ok") and isinstance(data.get("meta"), dict):
                meta = dict(data["meta"])
                if "stream_target" in meta:
                    raw_st = meta.get("stream_target")
                    if raw_st is None:
                        meta.pop("stream_target", None)
                    else:
                        try:
                            meta["stream_target"] = validate_stream_target(str(raw_st))
                        except ValueError:
                            meta.pop("stream_target", None)
                return meta
    except Exception:  # noqa: BLE001
        pass
    return None
