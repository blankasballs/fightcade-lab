"""Pure-Python Cloudflare R2 BYOC (Bring Your Own Cloud) SigV4 client & rolling-cap sync engine.

Zero `boto3` dependency: signs S3-compatible AWS SigV4 (`region="auto"`, `service="s3"`)
requests using Python stdlib (`hmac`, `hashlib`, `datetime`, `gzip`, `urllib.parse`) + `requests`.

Security & Cost Guarantees:
  - Strict regex validation for Cloudflare Account ID (32 hex), R2 Bucket Name, Access Key ID,
    and relative `replays/<quark_id>/<filename>` keys (SSRF & path-traversal safe).
  - Local credentials live strictly inside gitignored `data/byoc_r2.json` and are NEVER returned
    in plaintext via any HTTP API.
  - Supports an optional separate Read-Only R2 token so `fightcadelab.com` only ever receives
    read-only signing credentials while the local PC keeps upload/delete rights.
  - Auto-configures bucket CORS (`PutBucketCors`) on connect so mobile browsers can stream
    MP4s and fetch gzipped overlays directly from the user's R2 bucket.
  - Enforces a strict rolling storage cap (default `9.0 GB`, strictly under Cloudflare R2's
    `10 GB` free tier) by evicting the oldest synced remote sets before uploading new ones,
    without ever deleting local PC files or shared Discord clips.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape as xml_escape

import requests

from fightcade_analysis.annotations import create_annotation, list_annotations, update_annotation
from fightcade_analysis.companion_bridge import get_cloud_base_url, get_cloud_token
from fightcade_analysis.env_config import DEFAULT_DATA_DIR
from fightcade_analysis.fetch_replays import DEFAULT_UA
from fightcade_analysis.round_renderer import mobile_manifest_path

R2_ACCOUNT_ID_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
R2_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
R2_ACCESS_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
BYOC_KEY_RE = re.compile(r"^replays/[A-Za-z0-9_-]{3,64}/[A-Za-z0-9_.-]{1,120}$")
QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

DEFAULT_BYOC_CAP_GB: float = 9.0
MIN_BYOC_CAP_GB: float = 0.5
MAX_BYOC_CAP_GB: float = 500.0
BYTES_PER_GB: int = 1_000_000_000  # Decimal GB (9.0 GB = 9,000,000,000 B, safely under R2 10 GB free tier)
MAX_MANIFEST_BYTES: int = 256 * 1024

_BYOC_LOCK = threading.Lock()
_SYNC_STATE_LOCK = threading.Lock()
_SYNC_RUNTIME: dict[str, Any] = {
    "syncing": False,
    "last_error": None,
    "last_sync_at": None,
    "active_quark_id": None,
}


class ByocError(RuntimeError):
    """Raised when an R2 or cloud BYOC API call fails."""

    def __init__(self, message: str, *, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = code


class ByocNotConnectedError(ByocError):
    def __init__(self, message: str = "BYOC Cloudflare R2 is not configured yet.") -> None:
        super().__init__(message, status_code=400, code="byoc_not_connected")


class ByocCloudAuthError(ByocError):
    def __init__(self, message: str = "Connect your Fightcade Lab cloud account first.") -> None:
        super().__init__(message, status_code=401, code="cloud_not_connected")


# ---------------------------------------------------------------------------
# Strict Validation & Masking Helpers
# ---------------------------------------------------------------------------

def validate_account_id(raw: Any) -> str:
    clean = str(raw or "").strip().lower()
    if not R2_ACCOUNT_ID_RE.match(clean):
        raise ValueError("account_id must be a valid 32-character hexadecimal Cloudflare Account ID")
    return clean


def validate_bucket_name(raw: Any) -> str:
    clean = str(raw or "").strip().lower()
    if not R2_BUCKET_RE.match(clean):
        raise ValueError("bucket_name must be 3-63 lowercase letters, digits, or hyphens")
    return clean


def validate_access_key_id(raw: Any, *, field_name: str = "access_key_id") -> str:
    clean = str(raw or "").strip()
    if not R2_ACCESS_KEY_RE.match(clean):
        raise ValueError(f"{field_name} must be 8-128 alphanumeric, hyphen, or underscore characters")
    return clean


def validate_secret_access_key(raw: Any, *, field_name: str = "secret_access_key") -> str:
    clean = str(raw or "").strip()
    if len(clean) < 8 or len(clean) > 256:
        raise ValueError(f"{field_name} must be 8-256 characters")
    return clean


def validate_byoc_key(raw: Any) -> str:
    clean = str(raw or "").strip()
    if not clean or clean.startswith("/") or ".." in clean or "\\" in clean or not BYOC_KEY_RE.match(clean):
        raise ValueError(f"Invalid BYOC R2 object key: {clean!r}")
    return clean


def clamp_cap_gb(raw: Any, default: float = DEFAULT_BYOC_CAP_GB) -> float:
    if raw is None or raw == "":
        return float(default)
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("cap_gb must be a number") from exc
    if not (val == val):  # NaN
        raise ValueError("cap_gb must be a number")
    return round(max(MIN_BYOC_CAP_GB, min(MAX_BYOC_CAP_GB, val)), 2)


def mask_account_id(account_id: str) -> str:
    s = str(account_id or "").strip()
    if len(s) < 8:
        return "••••"
    return f"{s[:4]}••••{s[-4:]}"


def mask_access_key_id(access_key_id: str) -> str:
    s = str(access_key_id or "").strip()
    if len(s) < 4:
        return "••••"
    return f"••••{s[-4:]}"


def build_r2_endpoint(account_id: str, bucket_name: str, key: str = "") -> str:
    """Build SSRF-safe R2 S3 API URL strictly on `<32-hex>.r2.cloudflarestorage.com`."""
    acct = validate_account_id(account_id)
    bucket = validate_bucket_name(bucket_name)
    base = f"https://{acct}.r2.cloudflarestorage.com/{bucket}"
    if key:
        clean_key = validate_byoc_key(key)
        encoded_key = "/".join(quote(part, safe="-_.~") for part in clean_key.split("/"))
        return f"{base}/{encoded_key}"
    return base


# ---------------------------------------------------------------------------
# Pure-Python AWS SigV4 Signer & S3 Operations (`region="auto"`, `service="s3"`)
# ---------------------------------------------------------------------------

def _rfc3986_quote(value: str) -> str:
    return quote(str(value), safe="-_.~")


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_access_key: str, date_stamp: str, region: str = "auto", service: str = "s3") -> bytes:
    k_date = _hmac_sha256(f"AWS4{secret_access_key}".encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def sign_r2_request(
    *,
    method: str,
    account_id: str,
    bucket_name: str,
    access_key_id: str,
    secret_access_key: str,
    key: str = "",
    query_params: dict[str, str] | None = None,
    payload: bytes = b"",
    extra_headers: dict[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Sign an S3-compatible R2 HTTP request with AWS SigV4 and return `(url, headers)`."""
    acct = validate_account_id(account_id)
    bucket = validate_bucket_name(bucket_name)
    method_up = method.strip().upper()
    if method_up not in ("HEAD", "GET", "PUT", "DELETE", "POST"):
        raise ValueError(f"Unsupported HTTP method: {method_up}")

    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = dt.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = dt.strftime("%Y%m%d")
    region = "auto"
    service = "s3"

    host = f"{acct}.r2.cloudflarestorage.com"
    if key:
        clean_key = validate_byoc_key(key)
        encoded_key = "/".join(_rfc3986_quote(seg) for seg in clean_key.split("/"))
        canonical_uri = f"/{_rfc3986_quote(bucket)}/{encoded_key}"
    else:
        canonical_uri = f"/{_rfc3986_quote(bucket)}"

    q_items = sorted((str(k), str(v)) for k, v in (query_params or {}).items())
    canonical_query = "&".join(f"{_rfc3986_quote(k)}={_rfc3986_quote(v)}" for k, v in q_items)

    payload_hash = hashlib.sha256(payload).hexdigest()

    headers_lower: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    out_headers: dict[str, str] = {
        "Host": host,
        "X-Amz-Content-Sha256": payload_hash,
        "X-Amz-Date": amz_date,
        "User-Agent": DEFAULT_UA,
    }
    for hk, hv in (extra_headers or {}).items():
        clean_k = str(hk).strip()
        clean_v = str(hv).strip()
        if clean_k and clean_v:
            headers_lower[clean_k.lower()] = clean_v
            out_headers[clean_k] = clean_v

    sorted_header_keys = sorted(headers_lower.keys())
    canonical_headers = "".join(f"{k}:{headers_lower[k]}\n" for k in sorted_header_keys)
    signed_headers = ";".join(sorted_header_keys)

    canonical_request = "\n".join(
        [
            method_up,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    sig = hmac.new(
        _signing_key(secret_access_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    out_headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}"
    )

    url = f"https://{host}{canonical_uri}"
    if canonical_query:
        url = f"{url}?{canonical_query}"
    return url, out_headers


def default_cors_origins() -> list[str]:
    origins = ["https://fightcadelab.com", "https://www.fightcadelab.com"]
    cloud_url = get_cloud_base_url()
    parsed = urlsplit(cloud_url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        cloud_origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
        if cloud_origin not in origins:
            origins.append(cloud_origin)
    return origins


def build_cors_xml(allowed_origins: list[str] | None = None) -> bytes:
    origins = [o.strip().rstrip("/") for o in (allowed_origins or default_cors_origins()) if str(o).strip()]
    if not origins:
        origins = default_cors_origins()
    origin_tags = "\n".join(f"    <AllowedOrigin>{xml_escape(o)}</AllowedOrigin>" for o in origins)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<CORSConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">\n'
        "  <CORSRule>\n"
        f"{origin_tags}\n"
        "    <AllowedMethod>GET</AllowedMethod>\n"
        "    <AllowedMethod>HEAD</AllowedMethod>\n"
        "    <AllowedHeader>*</AllowedHeader>\n"
        "    <ExposeHeader>ETag</ExposeHeader>\n"
        "    <ExposeHeader>Content-Length</ExposeHeader>\n"
        "    <ExposeHeader>Content-Range</ExposeHeader>\n"
        "    <ExposeHeader>Accept-Ranges</ExposeHeader>\n"
        "    <MaxAgeSeconds>86400</MaxAgeSeconds>\n"
        "  </CORSRule>\n"
        "</CORSConfiguration>\n"
    )
    return xml.encode("utf-8")


def _http_client(session: requests.Session | None) -> Any:
    return session if session is not None else requests


def head_bucket(
    cfg: dict[str, Any],
    *,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> bool:
    """Verify R2 credentials and bucket reachability via S3 `HeadBucket`."""
    url, headers = sign_r2_request(
        method="HEAD",
        account_id=cfg["account_id"],
        bucket_name=cfg["bucket_name"],
        access_key_id=cfg["access_key_id"],
        secret_access_key=cfg["secret_access_key"],
    )
    try:
        resp = _http_client(session).head(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ByocError(f"Could not reach Cloudflare R2 bucket: {exc}", status_code=502) from exc
    if resp.status_code == 200:
        return True
    if resp.status_code in (401, 403):
        raise ByocError("R2 credentials were rejected (check Account ID, Bucket Name, and API token permissions).", status_code=400)
    if resp.status_code == 404:
        raise ByocError(f"R2 bucket '{cfg['bucket_name']}' was not found.", status_code=400)
    raise ByocError(f"R2 bucket check failed (HTTP {resp.status_code}).", status_code=502)


def put_bucket_cors(
    cfg: dict[str, Any],
    allowed_origins: list[str] | None = None,
    *,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> bool:
    """Automatically configure S3 CORS XML on the user's R2 bucket for direct mobile streaming."""
    xml_bytes = build_cors_xml(allowed_origins)
    content_md5 = base64.b64encode(hashlib.md5(xml_bytes).digest()).decode("ascii")  # noqa: S324
    url, headers = sign_r2_request(
        method="PUT",
        account_id=cfg["account_id"],
        bucket_name=cfg["bucket_name"],
        access_key_id=cfg["access_key_id"],
        secret_access_key=cfg["secret_access_key"],
        query_params={"cors": ""},
        payload=xml_bytes,
        extra_headers={
            "Content-Type": "application/xml",
            "Content-MD5": content_md5,
        },
    )
    try:
        resp = _http_client(session).put(url, headers=headers, data=xml_bytes, timeout=timeout)
    except requests.RequestException as exc:
        raise ByocError(f"Failed to configure R2 CORS policy: {exc}", status_code=502) from exc
    if resp.status_code in (200, 204):
        return True
    if resp.status_code in (401, 403):
        raise ByocError(
            "R2 token lacks permission to configure CORS (grant Object Read & Write / Admin Read & Write on this bucket).",
            status_code=400,
        )
    raise ByocError(f"Failed to set R2 CORS policy (HTTP {resp.status_code}).", status_code=502)


def put_object(
    cfg: dict[str, Any],
    key: str,
    data_or_path: bytes | Path | str,
    content_type: str,
    *,
    content_encoding: str | None = None,
    session: requests.Session | None = None,
    timeout: float = 120.0,
) -> int:
    """Upload bytes or a local file to `replays/<qid>/<file>` in the user's R2 bucket."""
    clean_key = validate_byoc_key(key)
    if isinstance(data_or_path, (bytes, bytearray)):
        payload = bytes(data_or_path)
    else:
        payload = Path(data_or_path).read_bytes()

    extra: dict[str, str] = {"Content-Type": str(content_type)}
    if content_encoding:
        extra["Content-Encoding"] = str(content_encoding)

    url, headers = sign_r2_request(
        method="PUT",
        account_id=cfg["account_id"],
        bucket_name=cfg["bucket_name"],
        access_key_id=cfg["access_key_id"],
        secret_access_key=cfg["secret_access_key"],
        key=clean_key,
        payload=payload,
        extra_headers=extra,
    )
    try:
        resp = _http_client(session).put(url, headers=headers, data=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise ByocError(f"R2 upload failed for {clean_key}: {exc}", status_code=502) from exc
    if resp.status_code not in (200, 201, 204):
        raise ByocError(f"R2 upload failed for {clean_key} (HTTP {resp.status_code}).", status_code=502)
    return len(payload)


def delete_object(
    cfg: dict[str, Any],
    key: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 30.0,
) -> bool:
    """Delete an evicted object from the user's R2 bucket."""
    clean_key = validate_byoc_key(key)
    url, headers = sign_r2_request(
        method="DELETE",
        account_id=cfg["account_id"],
        bucket_name=cfg["bucket_name"],
        access_key_id=cfg["access_key_id"],
        secret_access_key=cfg["secret_access_key"],
        key=clean_key,
    )
    try:
        resp = _http_client(session).delete(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ByocError(f"R2 delete failed for {clean_key}: {exc}", status_code=502) from exc
    if resp.status_code in (200, 204, 404):
        return True
    raise ByocError(f"R2 delete failed for {clean_key} (HTTP {resp.status_code}).", status_code=502)


# ---------------------------------------------------------------------------
# Local Config & Sync Ledger (`data/byoc_r2.json`)
# ---------------------------------------------------------------------------

def byoc_config_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "byoc_r2.json"


def load_byoc_config(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    """Load local BYOC R2 config + sync ledger (server-side only — never serialize directly to HTTP)."""
    path = byoc_config_path(data_dir)
    with _BYOC_LOCK:
        if not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(doc, dict) or not doc.get("account_id") or not doc.get("bucket_name"):
        return None
    doc.setdefault("cap_gb", DEFAULT_BYOC_CAP_GB)
    doc.setdefault("auto_sync", True)
    if not isinstance(doc.get("synced_matches"), dict):
        doc["synced_matches"] = {}
    return doc


def save_byoc_config(doc: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> None:
    path = byoc_config_path(data_dir)
    with _BYOC_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp_{os.getpid()}_{threading.get_ident()}")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def _set_sync_runtime(**updates: Any) -> None:
    with _SYNC_STATE_LOCK:
        _SYNC_RUNTIME.update(updates)


def get_byoc_status(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Return safe BYOC status summary for UI/API (NEVER exposes secret keys)."""
    cfg = load_byoc_config(data_dir)
    with _SYNC_STATE_LOCK:
        rt = dict(_SYNC_RUNTIME)

    if not cfg:
        return {
            "ok": True,
            "connected": False,
            "provider": "r2",
            "account_id_masked": None,
            "bucket_name": None,
            "access_key_id_masked": None,
            "has_separate_read_token": False,
            "cap_gb": DEFAULT_BYOC_CAP_GB,
            "auto_sync": True,
            "used_bytes": 0,
            "synced_count": 0,
            "synced_quark_ids": [],
            "syncing": bool(rt.get("syncing")),
            "last_error": rt.get("last_error"),
            "last_sync_at": rt.get("last_sync_at"),
            "updated_at": None,
        }

    synced = cfg.get("synced_matches") or {}
    used_bytes = sum(int(v.get("total_bytes") or 0) for v in synced.values() if isinstance(v, dict))
    return {
        "ok": True,
        "connected": True,
        "provider": "r2",
        "account_id_masked": mask_account_id(str(cfg.get("account_id") or "")),
        "bucket_name": str(cfg.get("bucket_name") or ""),
        "access_key_id_masked": mask_access_key_id(str(cfg.get("access_key_id") or "")),
        "has_separate_read_token": bool(cfg.get("read_access_key_id") and cfg.get("read_secret_access_key")),
        "cap_gb": float(cfg.get("cap_gb", DEFAULT_BYOC_CAP_GB)),
        "auto_sync": bool(cfg.get("auto_sync", True)),
        "used_bytes": used_bytes,
        "synced_count": len(synced),
        "synced_quark_ids": sorted(str(k) for k in synced.keys()),
        "syncing": bool(rt.get("syncing")),
        "last_error": rt.get("last_error"),
        "last_sync_at": rt.get("last_sync_at"),
        "updated_at": cfg.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Cloud API Helpers (using Companion's paired `cloud_token`)
# ---------------------------------------------------------------------------

def _require_cloud_token(data_dir: Path) -> str:
    tok = get_cloud_token(data_dir)
    if not tok:
        raise ByocCloudAuthError(
            "Connect your Fightcade Lab cloud account first so your phone can sign in."
        )
    return tok


def _cloud_request(
    method: str,
    path: str,
    *,
    cloud_token: str,
    json_body: dict[str, Any] | None = None,
    session: requests.Session | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    url = f"{get_cloud_base_url()}{path}"
    headers = {
        "Authorization": f"Bearer {cloud_token}",
        "Content-Type": "application/json",
        "User-Agent": DEFAULT_UA,
    }
    try:
        resp = _http_client(session).request(
            method.upper(),
            url,
            headers=headers,
            json=json_body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ByocError(f"Could not reach Fightcade Lab cloud ({path}): {exc}", status_code=502) from exc

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {"ok": False, "error": f"HTTP {resp.status_code}"}

    if resp.status_code == 401:
        raise ByocCloudAuthError(str(data.get("error") or "Cloud token expired; reconnect your account."))
    if not resp.ok or data.get("ok") is False:
        raise ByocError(
            str(data.get("error") or f"Cloud API error ({resp.status_code})"),
            status_code=resp.status_code,
            code=data.get("code"),
        )
    return data


# ---------------------------------------------------------------------------
# Connect / Settings / Disconnect
# ---------------------------------------------------------------------------

def connect_byoc(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Validate credentials, verify R2 bucket, configure CORS, push read creds to cloud, and save locally."""
    cloud_token = _require_cloud_token(data_dir)
    existing = load_byoc_config(data_dir) or {}

    account_id = validate_account_id(payload.get("account_id") or existing.get("account_id"))
    bucket_name = validate_bucket_name(payload.get("bucket_name") or existing.get("bucket_name"))
    access_key_id = validate_access_key_id(payload.get("access_key_id") or existing.get("access_key_id"))
    raw_secret = str(payload.get("secret_access_key") or "").strip()
    if raw_secret:
        secret_access_key = validate_secret_access_key(raw_secret)
    elif existing.get("secret_access_key"):
        secret_access_key = str(existing["secret_access_key"])
    else:
        raise ValueError("secret_access_key is required")

    raw_read_ak = str(payload.get("read_access_key_id") or "").strip()
    raw_read_sk = str(payload.get("read_secret_access_key") or "").strip()
    if raw_read_ak or raw_read_sk:
        read_access_key_id: str | None = validate_access_key_id(raw_read_ak, field_name="read_access_key_id")
        read_secret_access_key: str | None = validate_secret_access_key(raw_read_sk, field_name="read_secret_access_key")
    else:
        read_access_key_id = None
        read_secret_access_key = None

    cap_gb = clamp_cap_gb(payload.get("cap_gb"), default=float(existing.get("cap_gb", DEFAULT_BYOC_CAP_GB)))
    auto_sync = bool(payload.get("auto_sync", existing.get("auto_sync", True)))

    cfg_candidate: dict[str, Any] = {
        "schema_version": 1,
        "provider": "r2",
        "account_id": account_id,
        "bucket_name": bucket_name,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "read_access_key_id": read_access_key_id,
        "read_secret_access_key": read_secret_access_key,
        "cap_gb": cap_gb,
        "auto_sync": auto_sync,
        "synced_matches": (
            dict(existing.get("synced_matches") or {})
            if existing.get("account_id") == account_id and existing.get("bucket_name") == bucket_name
            else {}
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    # 1. Verify credentials & bucket reachability
    head_bucket(cfg_candidate, session=session)

    # 2. Configure CORS automatically so mobile browsers can stream MP4 & fetch overlays
    put_bucket_cors(cfg_candidate, session=session)

    # 3. Push Read-Only token (if provided) or primary token to fightcadelab.com
    cloud_ak = read_access_key_id or access_key_id
    cloud_sk = read_secret_access_key or secret_access_key
    _cloud_request(
        "PUT",
        "/api/byoc/config",
        cloud_token=cloud_token,
        json_body={
            "account_id": account_id,
            "bucket_name": bucket_name,
            "access_key_id": cloud_ak,
            "secret_access_key": cloud_sk,
            "cap_gb": cap_gb,
        },
        session=session,
    )

    save_byoc_config(cfg_candidate, data_dir=data_dir)
    _set_sync_runtime(last_error=None)
    return get_byoc_status(data_dir)


def update_byoc_settings(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Update `auto_sync` and/or `cap_gb` locally and sync `cap_gb` to cloud."""
    cfg = load_byoc_config(data_dir)
    if not cfg:
        raise ByocNotConnectedError()

    cap_changed = False
    if "auto_sync" in payload:
        cfg["auto_sync"] = bool(payload["auto_sync"])
    if "cap_gb" in payload:
        new_cap = clamp_cap_gb(payload["cap_gb"], default=float(cfg.get("cap_gb", DEFAULT_BYOC_CAP_GB)))
        if new_cap != float(cfg.get("cap_gb", DEFAULT_BYOC_CAP_GB)):
            cfg["cap_gb"] = new_cap
            cap_changed = True

    if cap_changed:
        cloud_token = get_cloud_token(data_dir)
        if cloud_token:
            _cloud_request(
                "PUT",
                "/api/byoc/config",
                cloud_token=cloud_token,
                json_body={"cap_gb": cfg["cap_gb"]},
                session=session,
            )

    cfg["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_byoc_config(cfg, data_dir=data_dir)
    return get_byoc_status(data_dir)


def disconnect_byoc(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Remove local BYOC config and disconnect R2 credentials on fightcadelab.com."""
    cloud_token = get_cloud_token(data_dir)
    if cloud_token:
        try:
            _cloud_request("DELETE", "/api/byoc/config", cloud_token=cloud_token, session=session)
        except ByocError:
            pass
    path = byoc_config_path(data_dir)
    with _BYOC_LOCK:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _set_sync_runtime(syncing=False, last_error=None, active_quark_id=None)
    return get_byoc_status(data_dir)


# ---------------------------------------------------------------------------
# Set Upload, 9.0 GB Rolling Auto-Prune & Cloud Manifest Registration
# ---------------------------------------------------------------------------

def _locate_local_manifest(quark_id: str, data_dir: Path) -> Path:
    """Resolve local manifest at `data/replays/<qid>/manifest.json` (with `data/mobile/<qid>/` fallback)."""
    primary = mobile_manifest_path(quark_id, data_dir=data_dir)
    if primary.is_file():
        return primary
    alt = data_dir.resolve() / "mobile" / quark_id / "manifest.json"
    if alt.is_file():
        return alt
    raise FileNotFoundError(f"Local replay manifest not found for {quark_id}")


def _build_compact_cloud_manifest(
    local_mf: dict[str, Any],
    quark_id: str,
    round_key_map: dict[int, tuple[str, str | None]],
) -> dict[str, Any]:
    """Build a compact manifest (`<= 256 KB`) replacing local URLs with validated R2 keys."""
    compact_rounds: list[dict[str, Any]] = []
    for raw_r in local_mf.get("rounds") or []:
        rid = int(raw_r.get("round_id") or 0)
        if rid not in round_key_map:
            continue
        vid_key, ov_key = round_key_map[rid]
        r_out: dict[str, Any] = {
            "round_id": rid,
            "match_id": int(raw_r.get("match_id") or 1),
            "match_round": int(raw_r.get("match_round") or rid),
            "start_gf": int(raw_r.get("start_gf") or 0),
            "end_gf": int(raw_r.get("end_gf") or 0),
            "frame_count": int(raw_r.get("frame_count") or 0),
            "fps": int(raw_r.get("fps") or 60),
            "duration_s": float(raw_r.get("duration_s") or 0.0),
            "p1_char": int(raw_r.get("p1_char") or 0),
            "p2_char": int(raw_r.get("p2_char") or 0),
            "p1_char_name": str(raw_r.get("p1_char_name") or ""),
            "p2_char_name": str(raw_r.get("p2_char_name") or ""),
            "winner": int(raw_r.get("winner") or 0),
            "video_r2_key": vid_key,
            "hp_curve": raw_r.get("hp_curve_raw") if isinstance(raw_r.get("hp_curve_raw"), list) else (raw_r.get("hp_curve") or []),
            "markers": [
                m for m in (raw_r.get("markers") or [])
                if isinstance(m, dict) and str(m.get("kind") or m.get("type") or "") != "select_mark"
            ],
        }
        if ov_key:
            r_out["overlay_r2_key"] = ov_key
        compact_rounds.append(r_out)

    cloud_mf: dict[str, Any] = {
        "version": int(local_mf.get("version") or 3),
        "quark_id": quark_id,
        "perspective": int(local_mf.get("perspective") or 1),
        "p1": local_mf.get("p1") or {
            "side": 1,
            "name": str(local_mf.get("p1_player_name") or local_mf.get("self_player_name") or "P1"),
            "rank_letter": str(local_mf.get("p1_rank_letter") or ""),
            "char_id": int(local_mf.get("p1_char") or 0),
            "char_name": str(local_mf.get("p1_char_name") or ""),
            "chars": list(local_mf.get("p1_chars") or []),
        },
        "p2": local_mf.get("p2") or {
            "side": 2,
            "name": str(local_mf.get("p2_player_name") or local_mf.get("opp_player_name") or "P2"),
            "rank_letter": str(local_mf.get("p2_rank_letter") or ""),
            "char_id": int(local_mf.get("p2_char") or 0),
            "char_name": str(local_mf.get("p2_char_name") or ""),
            "chars": list(local_mf.get("p2_chars") or []),
        },
        "total_duration_s": float(local_mf.get("total_duration_s") or 0.0),
        "rounds": compact_rounds,
    }

    # Ensure strictly <= 256 KB serialized JSON (downsample hp_curve if an unusually long FT20 set exceeds it)
    raw_bytes = json.dumps(cloud_mf, separators=(",", ":")).encode("utf-8")
    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        for r in cloud_mf["rounds"]:
            curve = r.get("hp_curve") or []
            if len(curve) > 40:
                r["hp_curve"] = curve[::2] + ([curve[-1]] if curve[-1] != curve[::2][-1] else [])
    return cloud_mf


def _epoch_ms_from_qid_or_manifest(quark_id: str, local_mf: dict[str, Any], mf_path: Path) -> int:
    raw_date = local_mf.get("date")
    if isinstance(raw_date, (int, float)) and raw_date > 0:
        return int(raw_date)
    m = re.match(r"^(\d{12,14})(?:-|$)", quark_id)
    if m:
        return int(m.group(1))
    try:
        return int(mf_path.stat().st_mtime * 1000)
    except OSError:
        return int(datetime.now(timezone.utc).timestamp() * 1000)


def sync_match_to_byoc(
    quark_id: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Upload one rendered match set (MP4s + gzipped overlays) to user's R2 bucket with strict rolling cap."""
    qid = str(quark_id or "").strip()
    if not QUARK_ID_RE.match(qid):
        raise ValueError(f"Invalid quark_id: {qid!r}")

    cfg = load_byoc_config(data_dir)
    if not cfg:
        raise ByocNotConnectedError()
    cloud_token = _require_cloud_token(data_dir)

    mf_path = _locate_local_manifest(qid, data_dir)
    rep_dir = mf_path.parent
    local_mf = json.loads(mf_path.read_text(encoding="utf-8"))
    rounds = local_mf.get("rounds") or []
    if not rounds:
        raise ValueError(f"Manifest for {qid} has no rounds to sync")

    # Prepare upload queue (MP4s + gzipped overlays) & calculate exact byte budget BEFORE uploading
    uploads: list[tuple[str, bytes | Path, str, str | None, int]] = []
    round_key_map: dict[int, tuple[str, str | None]] = {}
    set_bytes = 0

    for r in rounds:
        rid = int(r.get("round_id") or 0)
        if rid < 1:
            continue
        mp4_name = str(r.get("filename") or f"r{rid:02d}.mp4")
        mp4_path = rep_dir / mp4_name
        if not mp4_path.is_file():
            continue
        mp4_size = mp4_path.stat().st_size
        vid_key = validate_byoc_key(f"replays/{qid}/r{rid:02d}.mp4")
        uploads.append((vid_key, mp4_path, "video/mp4", None, mp4_size))
        set_bytes += mp4_size

        ov_key: str | None = None
        ov_gz_path = rep_dir / f"r{rid:02d}_overlay.json.gz"
        ov_json_path = rep_dir / f"r{rid:02d}_overlay.json"
        if ov_gz_path.is_file():
            ov_bytes = ov_gz_path.read_bytes()
            ov_key = validate_byoc_key(f"replays/{qid}/r{rid:02d}_overlay.json.gz")
            uploads.append((ov_key, ov_bytes, "application/gzip", "gzip", len(ov_bytes)))
            set_bytes += len(ov_bytes)
        elif ov_json_path.is_file():
            ov_bytes = gzip.compress(ov_json_path.read_bytes(), compresslevel=6)
            ov_key = validate_byoc_key(f"replays/{qid}/r{rid:02d}_overlay.json.gz")
            uploads.append((ov_key, ov_bytes, "application/gzip", "gzip", len(ov_bytes)))
            set_bytes += len(ov_bytes)

        round_key_map[rid] = (vid_key, ov_key)

    if not round_key_map:
        raise FileNotFoundError(f"No round MP4 files found for {qid}")

    # Enforce strict rolling cap_gb BEFORE upload (evicting oldest remote sets only; never local files!)
    cap_gb = float(cfg.get("cap_gb", DEFAULT_BYOC_CAP_GB))
    cap_bytes = int(cap_gb * BYTES_PER_GB)
    synced_matches: dict[str, dict[str, Any]] = dict(cfg.get("synced_matches") or {})
    evicted: list[str] = []

    def _current_other_bytes() -> int:
        return sum(
            int(entry.get("total_bytes") or 0)
            for other_qid, entry in synced_matches.items()
            if other_qid != qid and isinstance(entry, dict)
        )

    candidates = sorted(
        ((oq, entry) for oq, entry in synced_matches.items() if oq != qid and isinstance(entry, dict)),
        key=lambda kv: (int(kv[1].get("played_at") or 0), str(kv[1].get("synced_at") or ""), kv[0]),
    )
    while _current_other_bytes() + set_bytes > cap_bytes and candidates:
        old_qid, old_entry = candidates.pop(0)
        for old_key in old_entry.get("keys") or []:
            try:
                delete_object(cfg, str(old_key), session=session)
            except ByocError:
                pass
        try:
            _cloud_request("DELETE", f"/api/byoc/matches/{old_qid}", cloud_token=cloud_token, session=session)
        except ByocError:
            pass
        synced_matches.pop(old_qid, None)
        evicted.append(old_qid)

    if evicted:
        cfg["synced_matches"] = synced_matches
        save_byoc_config(cfg, data_dir=data_dir)

    # Upload all objects for this match set
    uploaded_keys: list[str] = []
    for key, src, ctype, cenc, _sz in uploads:
        put_object(cfg, key, src, ctype, content_encoding=cenc, session=session)
        uploaded_keys.append(key)

    cloud_mf = _build_compact_cloud_manifest(local_mf, qid, round_key_map)
    p1_info = cloud_mf.get("p1") or {}
    p2_info = cloud_mf.get("p2") or {}
    played_at = _epoch_ms_from_qid_or_manifest(qid, local_mf, mf_path)
    p1_chars = list(p1_info.get("chars") or local_mf.get("p1_chars") or [])
    p2_chars = list(p2_info.get("chars") or local_mf.get("p2_chars") or [])
    p1_char = str(p1_info.get("char_name") or local_mf.get("p1_char_name") or (p1_chars[0] if p1_chars else ""))
    p2_char = str(p2_info.get("char_name") or local_mf.get("p2_char_name") or (p2_chars[0] if p2_chars else ""))
    has_overlay = any(ov is not None for _, ov in round_key_map.values())

    # Register synced set metadata + compact manifest in cloud D1
    _cloud_request(
        "POST",
        "/api/byoc/matches",
        cloud_token=cloud_token,
        json_body={
            "quark_id": qid,
            "game_id": str(local_mf.get("game_id") or "sfiii3nr1"),
            "played_at": played_at,
            "duration_s": float(cloud_mf.get("total_duration_s") or 0.0),
            "ranked": bool(local_mf.get("ranked", False)),
            "p1_name": str(p1_info.get("name") or local_mf.get("p1_player_name") or ""),
            "p2_name": str(p2_info.get("name") or local_mf.get("p2_player_name") or ""),
            "p1_rank": str(p1_info.get("rank_letter") or local_mf.get("p1_rank_letter") or ""),
            "p2_rank": str(p2_info.get("rank_letter") or local_mf.get("p2_rank_letter") or ""),
            "p1_char": p1_char,
            "p2_char": p2_char,
            "p1_chars": p1_chars,
            "p2_chars": p2_chars,
            "p1_score": local_mf.get("p1_score"),
            "p2_score": local_mf.get("p2_score"),
            "perspective": int(cloud_mf.get("perspective") or 1),
            "byoc_bytes": set_bytes,
            "round_count": len(round_key_map),
            "has_overlay_data": has_overlay,
            "manifest": cloud_mf,
        },
        session=session,
    )

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    synced_matches[qid] = {
        "synced_at": now_iso,
        "played_at": played_at,
        "total_bytes": set_bytes,
        "round_count": len(round_key_map),
        "keys": uploaded_keys,
    }
    cfg["synced_matches"] = synced_matches
    cfg["updated_at"] = now_iso
    save_byoc_config(cfg, data_dir=data_dir)
    _set_sync_runtime(last_error=None, last_sync_at=now_iso)

    return {
        "ok": True,
        "quark_id": qid,
        "uploaded_objects": len(uploaded_keys),
        "total_bytes": set_bytes,
        "round_count": len(round_key_map),
        "has_overlay_data": has_overlay,
        "evicted": evicted,
    }


def pull_cloud_notes_to_local(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Import mobile-created cloud moments into local `annotations.jsonl` without duplicating."""
    cloud_token = _require_cloud_token(data_dir)
    res = _cloud_request("GET", "/api/moments", cloud_token=cloud_token, session=session)
    cloud_moments = res.get("moments") or []

    local_anns = list_annotations(data_dir)
    existing_cloud_ids = {
        str(a.get("cloud_moment_id")).strip()
        for a in local_anns
        if str(a.get("cloud_moment_id") or "").strip()
    }
    existing_tuples = {
        (
            str(a.get("quark_id") or "").strip(),
            int(a.get("round_id") or 0),
            int(a.get("anchor_gf") or 0),
            str(a.get("note") or "").strip(),
        )
        for a in local_anns
    }

    from fightcade_analysis.match_catalog import detect_default_username

    owner = detect_default_username(data_dir)
    imported = 0
    skipped = 0

    for cm in cloud_moments:
        if not isinstance(cm, dict):
            continue
        cm_id = str(cm.get("id") or "").strip()
        qid = str(cm.get("quark_id") or "").strip()
        round_id = int(cm.get("round_id") or 0)
        anchor_gf = int(cm.get("anchor_gf") or cm.get("start_gf") or 0)
        note_text = str(cm.get("note") or "").strip()

        if not qid or not QUARK_ID_RE.match(qid) or round_id < 1 or anchor_gf < 1:
            skipped += 1
            continue
        if cm_id and cm_id in existing_cloud_ids:
            skipped += 1
            continue
        dedup_key = (qid, round_id, anchor_gf, note_text)
        if dedup_key in existing_tuples:
            skipped += 1
            continue

        start_gf = int(cm.get("start_gf") or max(1, anchor_gf - 120))
        end_gf = int(cm.get("end_gf") or (start_gf + 90))
        if end_gf <= start_gf:
            end_gf = start_gf + 90

        kind = str(cm.get("kind") or "note").strip().lower()
        if kind not in ("mistake", "question", "good_play", "opp_habit", "note", "response"):
            kind = "note"

        ann = create_annotation(
            {
                "quark_id": qid,
                "round_id": round_id,
                "perspective": 2 if int(cm.get("perspective") or 1) == 2 else 1,
                "anchor_gf": anchor_gf,
                "start_gf": start_gf,
                "end_gf": end_gf,
                "kind": kind,
                "tags": cm.get("tags") or [],
                "note": note_text,
                "queued_drill": False,
                "owner": owner,
            },
            data_dir=data_dir,
            async_bake=False,
        )
        if cm_id:
            patch: dict[str, Any] = {
                "cloud_moment_id": cm_id,
                "cloud_visibility": cm.get("visibility") or "private",
                "cloud_share_token": cm.get("share_token"),
                "cloud_share_url": cm.get("share_url"),
                "shared_caption": cm.get("shared_caption") or "",
                "include_note_on_share": bool(cm.get("include_note_on_share", False)),
                "hide_player_names": bool(cm.get("hide_player_names", False)),
            }
            update_annotation(ann["id"], patch, data_dir=data_dir, async_bake=False)
            existing_cloud_ids.add(cm_id)
        existing_tuples.add(dedup_key)
        imported += 1

    return {"ok": True, "imported": imported, "skipped": skipped}


def _list_recent_ready_quarks(data_dir: Path, limit: int = 5) -> list[str]:
    replays_root = data_dir.resolve() / "replays"
    if not replays_root.is_dir():
        return []
    items: list[tuple[float, str]] = []
    for d in replays_root.iterdir():
        if not d.is_dir() or not QUARK_ID_RE.match(d.name):
            continue
        mf = d / "manifest.json"
        if not mf.is_file():
            continue
        try:
            doc = json.loads(mf.read_text(encoding="utf-8"))
            if doc.get("in_progress") or not (doc.get("rounds") or []):
                continue
            items.append((mf.stat().st_mtime, d.name))
        except Exception:  # noqa: BLE001
            continue
    items.sort(reverse=True)
    return [qid for _, qid in items[: max(1, min(20, int(limit or 5)))]]


def trigger_background_byoc_sync(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    quark_id: str | None = None,
    limit: int = 5,
    only_if_auto: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Start a non-blocking background thread syncing one or recent matches + pulling mobile notes."""
    cfg = load_byoc_config(data_dir)
    if not cfg:
        return {"ok": False, "started": False, "reason": "byoc_not_connected"}
    if only_if_auto and not bool(cfg.get("auto_sync", True)):
        return {"ok": True, "started": False, "reason": "auto_sync_disabled"}
    if not get_cloud_token(data_dir):
        return {"ok": False, "started": False, "reason": "cloud_not_connected"}

    with _SYNC_STATE_LOCK:
        if _SYNC_RUNTIME.get("syncing"):
            return {"ok": True, "started": False, "already_syncing": True}
        _SYNC_RUNTIME["syncing"] = True
        _SYNC_RUNTIME["last_error"] = None
        _SYNC_RUNTIME["active_quark_id"] = quark_id

    targets = [str(quark_id).strip()] if quark_id else _list_recent_ready_quarks(data_dir, limit=limit)

    def _worker() -> None:
        try:
            for qid in targets:
                _set_sync_runtime(active_quark_id=qid)
                sync_match_to_byoc(qid, data_dir=data_dir, session=session)
            pull_cloud_notes_to_local(data_dir, session=session)
            _set_sync_runtime(last_error=None, last_sync_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        except Exception as exc:  # noqa: BLE001
            _set_sync_runtime(last_error=str(exc))
        finally:
            _set_sync_runtime(syncing=False, active_quark_id=None)

    t = threading.Thread(target=_worker, name="byoc-r2-sync", daemon=True)
    t.start()
    return {"ok": True, "started": True, "targets": targets}
