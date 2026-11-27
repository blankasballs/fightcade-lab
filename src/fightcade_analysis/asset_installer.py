"""Consent-based SHA-256 external asset installer & First-Run Onboarding helpers.

Licensing & SignPath Foundation compliance boundary:
  - Fightcade Lab (`AGPL-3.0-or-later`) NEVER bundles `fcadefbneo_fast.exe` (FBNeo license)
    or `ffmpeg.exe` (`GPL-3.0`) inside the signed Windows Companion installer.
  - Instead, the First-Run Wizard asks for explicit user consent, downloads external tools
    from their official upstream/fork GitHub releases, verifies exact SHA-256 checksums,
    and places verbatim license files alongside them.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable
from urllib.parse import urlsplit
import zipfile

import requests

from fightcade_analysis.companion_bridge import get_pairing_state
from fightcade_analysis.env_config import (
    DEFAULT_DATA_DIR,
    inspect_environment,
    load_app_config,
    require_local_ffmpeg,
    resolve_local_ffmpeg,
    save_app_config,
)
from fightcade_analysis.fetch_replays import DEFAULT_UA
from fightcade_analysis.ota_lua import ed25519_verify
from fightcade_analysis.uri_scheme import is_uri_scheme_registered

FAST_FBNEO_RELEASES_API = "https://api.github.com/repos/blankasballs/fightcade-fbneo-fast/releases/latest"
FAST_FBNEO_DOWNLOAD_PREFIX = "https://github.com/blankasballs/fightcade-fbneo-fast/releases/download/"
FFMPEG_DEFAULT_RELEASE_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_DEFAULT_SHA256_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"

_SHA256_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")
_SHA256_HEX_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def _require_https_url(url: str, *, allowed_prefix: str | None = None, label: str = "URL") -> str:
    """Validate that `url` uses HTTPS (and optionally matches an exact trusted prefix)."""
    clean = str(url or "").strip()
    if not clean or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in clean):
        raise ValueError(f"Invalid {label}: {url!r}")
    parsed = urlsplit(clean)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"{label} must use https:// scheme: {url!r}")
    if allowed_prefix and not clean.startswith(allowed_prefix):
        raise ValueError(f"Untrusted {label}: must start with {allowed_prefix}")
    return clean


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse standard GNU `SHA256SUMS.txt` into `{basename: lowercase_hex}`."""
    out: dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SHA256_LINE_RE.match(line)
        if m:
            digest = m.group(1).lower()
            fname = Path(m.group(2).strip()).name
            out[fname] = digest
    return out


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_verified_asset(
    url: str,
    dest_path: Path,
    *,
    expected_sha256: str,
    timeout_s: float = 120.0,
    max_bytes: int = 250 * 1024 * 1024,
    session: requests.Session | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Stream-download `url` to a temp file, verify exact SHA-256, and atomically move into place."""
    url = _require_https_url(url, label="asset download URL")
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or not all(c in "0123456789abcdef" for c in expected):
        raise ValueError("A valid 64-char hex expected_sha256 is required")

    dest = dest_path.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + f".part_{os.getpid()}")

    sess = session or requests.Session()
    h = hashlib.sha256()
    downloaded = 0
    try:
        with sess.get(url, headers={"User-Agent": DEFAULT_UA}, stream=True, timeout=timeout_s) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"Download failed with HTTP {resp.status_code} for {url}")
            total = int(resp.headers.get("Content-Length") or "0")
            with tmp.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ValueError(f"Downloaded asset exceeded {max_bytes} byte safety cap")
                    h.update(chunk)
                    out.write(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total)

        actual = h.hexdigest()
        if actual != expected:
            raise ValueError(f"SHA-256 checksum mismatch for {dest.name}: expected {expected[:16]}..., got {actual[:16]}...")

        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "path": str(dest),
        "size_bytes": downloaded,
        "sha256": actual,
    }


def install_fast_fbneo(
    fbneo_dir: Path,
    *,
    consent: bool = False,
    release_api_url: str = FAST_FBNEO_RELEASES_API,
    expected_sha256: str | None = None,
    sums_signature_b64: str | None = None,
    public_key_b64: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Consent-gated SHA-256 verified install of external `fcadefbneo_fast.exe` + FBNeo `license.txt`."""
    if not consent:
        raise PermissionError("Explicit user consent is required before downloading external fcadefbneo_fast.exe")
    target_dir = fbneo_dir.resolve()
    if not target_dir.is_dir():
        raise FileNotFoundError(f"Fightcade FBNeo directory does not exist: {target_dir}")

    release_api_url = _require_https_url(release_api_url, label="release API URL")
    sess = session or requests.Session()
    rel_resp = sess.get(release_api_url, headers={"User-Agent": DEFAULT_UA}, timeout=20.0)
    if rel_resp.status_code != 200:
        raise RuntimeError(f"Could not query fast-FBNeo release metadata (HTTP {rel_resp.status_code})")
    release = rel_resp.json()
    assets = {str(a.get("name") or ""): str(a.get("browser_download_url") or "") for a in (release.get("assets") or [])}

    sums_url = assets.get("SHA256SUMS.txt")
    exe_url = assets.get("fcadefbneo_fast.exe")
    lic_url = assets.get("license.txt")
    if not sums_url or not exe_url:
        raise RuntimeError("Release is missing fcadefbneo_fast.exe or SHA256SUMS.txt")

    sums_url = _require_https_url(
        sums_url, allowed_prefix=FAST_FBNEO_DOWNLOAD_PREFIX, label="SHA256SUMS.txt URL"
    )
    exe_url = _require_https_url(
        exe_url, allowed_prefix=FAST_FBNEO_DOWNLOAD_PREFIX, label="fcadefbneo_fast.exe URL"
    )
    if lic_url:
        lic_url = _require_https_url(
            lic_url, allowed_prefix=FAST_FBNEO_DOWNLOAD_PREFIX, label="license.txt URL"
        )

    sums_resp = sess.get(sums_url, headers={"User-Agent": DEFAULT_UA}, timeout=20.0)
    if sums_resp.status_code != 200:
        raise RuntimeError(f"Could not download SHA256SUMS.txt (HTTP {sums_resp.status_code})")

    if public_key_b64:
        sums_bytes = getattr(sums_resp, "content", None)
        if not isinstance(sums_bytes, (bytes, bytearray)):
            sums_bytes = str(sums_resp.text or "").encode("utf-8")
        sig_raw_b64 = sums_signature_b64
        if not sig_raw_b64:
            sig_url = assets.get("SHA256SUMS.txt.sig")
            if not sig_url:
                raise ValueError("Missing SHA256SUMS.txt.sig for Ed25519 checksum verification")
            sig_url = _require_https_url(
                sig_url, allowed_prefix=FAST_FBNEO_DOWNLOAD_PREFIX, label="SHA256SUMS.txt.sig URL"
            )
            sig_resp = sess.get(sig_url, headers={"User-Agent": DEFAULT_UA}, timeout=20.0)
            if sig_resp.status_code != 200:
                raise RuntimeError(f"Could not download SHA256SUMS.txt.sig (HTTP {sig_resp.status_code})")
            sig_content = getattr(sig_resp, "content", None)
            if isinstance(sig_content, (bytes, bytearray)) and len(sig_content) == 64:
                sig_bytes = bytes(sig_content)
            else:
                sig_raw_b64 = str(sig_resp.text or "").strip()
                sig_bytes = base64.b64decode(sig_raw_b64, validate=True)
        else:
            sig_bytes = base64.b64decode(str(sig_raw_b64).strip(), validate=True)
        pub_bytes = base64.b64decode(public_key_b64.strip(), validate=True)
        if not ed25519_verify(pub_bytes, bytes(sums_bytes), sig_bytes):
            raise ValueError("Ed25519 signature verification failed for SHA256SUMS.txt")

    checksums = parse_sha256sums(sums_resp.text)
    if "fcadefbneo_fast.exe" not in checksums:
        raise RuntimeError("SHA256SUMS.txt does not contain an entry for fcadefbneo_fast.exe")

    exe_expected_sha = checksums["fcadefbneo_fast.exe"]
    if expected_sha256 is not None:
        pinned = str(expected_sha256).strip().lower()
        if len(pinned) != 64 or not all(c in "0123456789abcdef" for c in pinned):
            raise ValueError("A valid 64-char hex expected_sha256 is required")
        if exe_expected_sha != pinned:
            raise ValueError(
                f"Pinned SHA-256 mismatch for fcadefbneo_fast.exe: expected {pinned[:16]}..., "
                f"SHA256SUMS.txt listed {exe_expected_sha[:16]}..."
            )
        exe_expected_sha = pinned

    exe_res = download_verified_asset(
        exe_url,
        target_dir / "fcadefbneo_fast.exe",
        expected_sha256=exe_expected_sha,
        session=sess,
    )

    license_installed = False
    if lic_url and "license.txt" in checksums:
        download_verified_asset(
            lic_url,
            target_dir / "fcadefbneo_fast_license.txt",
            expected_sha256=checksums["license.txt"],
            session=sess,
        )
        license_installed = True

    return {
        "ok": True,
        "tag": str(release.get("tag_name") or ""),
        "exe": exe_res,
        "license_installed": license_installed,
    }


def _parse_single_sha256(text: str) -> str:
    """Parse a 64-char hex SHA-256 checksum from a `.sha256` response body."""
    m = _SHA256_HEX_RE.search(str(text or "").strip())
    if not m:
        raise ValueError("Could not parse a valid 64-char hex SHA-256 digest from checksum response")
    return m.group(1).lower()


def install_ffmpeg_from_zip(
    data_dir: Path,
    *,
    consent: bool = False,
    zip_url: str = FFMPEG_DEFAULT_RELEASE_URL,
    expected_sha256: str | None = None,
    sha256_url: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Consent-gated SHA-256 verified extraction of upstream `ffmpeg.exe` (GPL-3.0) into `data/tools/`.

    When `expected_sha256` is omitted, fetches `<zip_url>.sha256` over HTTPS and verifies the archive
    against the published 64-hex SHA-256 checksum before extracting `ffmpeg.exe` and `LICENSE`.
    """
    if not consent:
        raise PermissionError("Explicit user consent is required before downloading external ffmpeg.exe")
    zip_url = _require_https_url(zip_url, label="FFmpeg zip URL")
    sess = session or requests.Session()

    eff_sha = expected_sha256
    if not eff_sha:
        checksum_url = _require_https_url(
            sha256_url or (FFMPEG_DEFAULT_SHA256_URL if zip_url == FFMPEG_DEFAULT_RELEASE_URL else f"{zip_url}.sha256"),
            label="FFmpeg SHA-256 URL",
        )
        sha_resp = sess.get(checksum_url, headers={"User-Agent": DEFAULT_UA}, timeout=20.0)
        if sha_resp.status_code != 200:
            raise RuntimeError(f"Could not fetch FFmpeg SHA-256 checksum (HTTP {sha_resp.status_code})")
        eff_sha = _parse_single_sha256(sha_resp.text)

    tools_dir = data_dir.resolve() / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    archive_path = tools_dir / "ffmpeg_download.zip"
    dl = download_verified_asset(zip_url, archive_path, expected_sha256=eff_sha, session=sess)
    extracted_exe = tools_dir / "ffmpeg.exe"
    license_installed = False
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = zf.namelist()
            target_member = next((m for m in names if m.lower().endswith("/ffmpeg.exe") or m.lower() == "ffmpeg.exe"), None)
            if not target_member:
                raise RuntimeError("Downloaded archive does not contain ffmpeg.exe")
            with zf.open(target_member) as src, extracted_exe.open("wb") as dst:
                shutil.copyfileobj(src, dst)

            lic_member = next(
                (m for m in names if Path(m).name.lower() in ("license", "license.txt") and not m.endswith("/")),
                None,
            )
            if lic_member:
                lic_dst = tools_dir / "ffmpeg_LICENSE.txt"
                with zf.open(lic_member) as src, lic_dst.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                license_installed = True
    finally:
        archive_path.unlink(missing_ok=True)

    return {
        "ok": True,
        "path": str(extracted_exe),
        "archive_sha256": dl["sha256"],
        "exe_sha256": compute_file_sha256(extracted_exe),
        "license_installed": license_installed,
    }


def install_ffmpeg_latest(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    consent: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Consent-gated download and SHA-256 verified install of the latest upstream FFmpeg Essentials build."""
    return install_ffmpeg_from_zip(
        data_dir,
        consent=consent,
        zip_url=FFMPEG_DEFAULT_RELEASE_URL,
        expected_sha256=None,
        sha256_url=FFMPEG_DEFAULT_SHA256_URL,
        session=session,
    )


def get_onboarding_status(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Aggregate the First-Run Wizard checklist without requiring any terminal commands."""
    env = inspect_environment(data_dir=data_dir)
    cfg = load_app_config(data_dir)
    ffmpeg_path = resolve_local_ffmpeg(data_dir)
    pairing = get_pairing_state(data_dir)
    replays_dir = data_dir.resolve() / "replays"
    has_processed = False
    if replays_dir.is_dir():
        has_processed = any((d / "manifest.json").is_file() for d in replays_dir.iterdir() if d.is_dir())

    return {
        "ok": True,
        "completed": bool(cfg.get("onboarding_completed", False)),
        "dismissed": bool(cfg.get("onboarding_dismissed", False)),
        "username": str(cfg.get("fightcade_username") or ""),
        "checks": {
            "fightcade_found": bool(env.get("fbneo_found")),
            "rom_found": bool(env.get("rom_found")),
            "grouflon_found": bool(env.get("has_grouflon")),
            "fast_fbneo_found": bool(env.get("fast_fbneo_found")),
            "ffmpeg_found": ffmpeg_path is not None,
            "uri_scheme_registered": is_uri_scheme_registered(),
            "browser_paired": bool(pairing.get("paired")),
            "has_processed_match": has_processed,
        },
        "paths": {
            "fbneo_dir": env.get("fbneo_dir"),
            "rom_path": env.get("rom_path"),
            "grouflon_dir": env.get("grouflon_dir"),
            "ffmpeg_path": str(ffmpeg_path) if ffmpeg_path else None,
        },
    }


def update_onboarding_state(updates: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "completed" in updates:
        clean["onboarding_completed"] = bool(updates["completed"])
    if "dismissed" in updates:
        clean["onboarding_dismissed"] = bool(updates["dismissed"])
    if "username" in updates:
        clean["fightcade_username"] = str(updates["username"] or "").strip()[:64]
    if clean:
        save_app_config(clean, data_dir)
    return get_onboarding_status(data_dir)
