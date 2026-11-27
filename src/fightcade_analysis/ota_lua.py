"""Signed Over-The-Air (OTA) Lua Script Packs with Ed25519 + SHA-256 verification.

Allows shipping new Takeover UX refinements and additional arcade game RAM maps
(`kof2002`, `vsav`, `ssf2t`) without rebuilding or re-signing the Windows Companion `.exe`.

Security guarantees:
  - Pure-Python RFC 8032 Ed25519 signature verification (zero external native crypto DLLs).
  - Canonical JSON manifest payload verified against pinned public key before writing anything.
  - Strict `.lua`-only relative filename allowlist (zero path traversal, max 512 KiB per script).
  - Per-file SHA-256 verification before atomic activation; any failure leaves the active pack untouched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from typing import Any, Callable

from fightcade_analysis.env_config import DEFAULT_DATA_DIR, REPO_ROOT

MAX_LUA_SCRIPT_BYTES = 512 * 1024
_GAME_ID_RE = re.compile(r"^[a-z0-9_-]{2,32}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z._-]{1,40}$")
_LUA_REL_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,120}\.lua$")

# ---------------------------------------------------------------------------
# Pure-Python RFC 8032 Ed25519 (zero external dependencies)
# ---------------------------------------------------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = _xrecover(_BY)
_B = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)
_NEUTRAL = (0, 1, 1, 0)


def _point_add(p1: tuple[int, int, int, int], p2: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    d = (2 * z1 * z2) % _P
    e = (b - a) % _P
    f = (d - c) % _P
    g = (d + c) % _P
    h = (b + a) % _P
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _point_mul(s: int, p: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    q = _NEUTRAL
    while s > 0:
        if s & 1:
            q = _point_add(q, p)
        p = _point_add(p, p)
        s >>= 1
    return q


def _point_equal(p1: tuple[int, int, int, int], p2: tuple[int, int, int, int]) -> bool:
    return (p1[0] * p2[2] - p2[0] * p1[2]) % _P == 0 and (p1[1] * p2[2] - p2[1] * p1[2]) % _P == 0


def _point_compress(p: tuple[int, int, int, int]) -> bytes:
    zinv = pow(p[2], _P - 2, _P)
    x = (p[0] * zinv) % _P
    y = (p[1] * zinv) % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _point_decompress(s: bytes) -> tuple[int, int, int, int] | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x = pow(u * pow(v, _P - 2, _P), (_P + 3) // 8, _P)
    if (v * x * x - u) % _P != 0:
        x = (x * _I) % _P
        if (v * x * x - u) % _P != 0:
            return None
    if x == 0 and sign == 1:
        return None
    if (x & 1) != sign:
        x = _P - x
    return (x, y, 1, (x * y) % _P)


def ed25519_generate_keypair() -> tuple[bytes, bytes]:
    """Generate `(private_seed_32b, public_key_32b)` for Ed25519."""
    seed = secrets.token_bytes(32)
    return seed, ed25519_public_key(seed)


def ed25519_public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 private seed must be 32 bytes")
    h = bytearray(_sha512(seed)[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    a = int.from_bytes(h, "little")
    return _point_compress(_point_mul(a, _B))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    """Sign `message` with a 32-byte Ed25519 seed, returning a 64-byte signature."""
    if len(seed) != 32:
        raise ValueError("Ed25519 private seed must be 32 bytes")
    h = _sha512(seed)
    a_bytes = bytearray(h[:32])
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    a = int.from_bytes(a_bytes, "little")
    prefix = h[32:]
    pub = _point_compress(_point_mul(a, _B))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    r_point = _point_compress(_point_mul(r, _B))
    k = int.from_bytes(_sha512(r_point + pub + message), "little") % _L
    s = (r + k * a) % _L
    return r_point + int.to_bytes(s, 32, "little")


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a 64-byte RFC 8032 Ed25519 signature."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a_pt = _point_decompress(public_key)
    r_pt = _point_decompress(signature[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = int.from_bytes(_sha512(signature[:32] + public_key + message), "little") % _L
    lhs = _point_mul(s, _B)
    rhs = _point_add(r_pt, _point_mul(k, a_pt))
    return _point_equal(lhs, rhs)


# ---------------------------------------------------------------------------
# Signed Lua Pack Manifests & Atomic Installer
# ---------------------------------------------------------------------------

def canonical_manifest_payload(manifest: dict[str, Any]) -> bytes:
    """Deterministic UTF-8 canonical JSON payload over all signed manifest fields (excluding signature)."""
    clean = {k: v for k, v in manifest.items() if k != "signature_b64"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validate_lua_rel_path(rel: str) -> str:
    norm = str(rel or "").replace("\\", "/").strip()
    if not norm or norm.startswith("/") or ".." in norm.split("/") or not _LUA_REL_PATH_RE.match(norm):
        raise ValueError(f"Unsafe or non-.lua script path in OTA manifest: '{rel}'")
    return norm


def build_signed_lua_pack_manifest(
    *,
    game_id: str,
    version: str,
    files: dict[str, bytes],
    private_seed: bytes,
    created_at: str = "",
) -> dict[str, Any]:
    """Create and Ed25519-sign a Lua pack manifest for `files = {rel_path: content_bytes}`."""
    if not _GAME_ID_RE.match(game_id):
        raise ValueError(f"Invalid game_id: '{game_id}'")
    if not _VERSION_RE.match(version):
        raise ValueError(f"Invalid version: '{version}'")
    if not files:
        raise ValueError("Lua pack must include at least one .lua file")

    entries = []
    for rel_path, content in sorted(files.items()):
        clean_rel = _validate_lua_rel_path(rel_path)
        if len(content) > MAX_LUA_SCRIPT_BYTES:
            raise ValueError(f"Script '{clean_rel}' exceeds {MAX_LUA_SCRIPT_BYTES} byte limit")
        entries.append(
            {
                "path": clean_rel,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "game_id": game_id,
        "version": version,
        "created_at": created_at or "2026-11-25T00:00:00Z",
        "files": entries,
    }
    sig = ed25519_sign(private_seed, canonical_manifest_payload(manifest))
    manifest["signature_b64"] = base64.b64encode(sig).decode("ascii")
    return manifest


def verify_manifest_signature(manifest: dict[str, Any], public_key_b64: str) -> bool:
    """Verify a manifest's Ed25519 signature against a base64-encoded 32-byte public key."""
    if not isinstance(manifest, dict):
        return False
    sig_b64 = str(manifest.get("signature_b64") or "").strip()
    if not sig_b64 or not public_key_b64:
        return False
    try:
        pub = base64.b64decode(public_key_b64.strip(), validate=True)
        sig = base64.b64decode(sig_b64, validate=True)
    except Exception:  # noqa: BLE001
        return False
    return ed25519_verify(pub, canonical_manifest_payload(manifest), sig)


def lua_packs_root(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "lua_packs"


def _parse_semver_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """Parse a numeric semver string into a comparable key `(nums, is_release, prerelease)`."""
    raw = str(version or "").strip()
    if raw.startswith(("v", "V")) and len(raw) > 1 and raw[1].isdigit():
        raw = raw[1:]
    core, _, prerelease = raw.partition("-")
    parts = core.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid numeric semver version: '{version}'")
    return (tuple(int(p) for p in parts), 0 if prerelease else 1, prerelease)


def _compare_semver(ver_a: str, ver_b: str) -> int:
    nums_a, rel_a, pre_a = _parse_semver_key(ver_a)
    nums_b, rel_b, pre_b = _parse_semver_key(ver_b)
    width = max(len(nums_a), len(nums_b))
    key_a = (nums_a + (0,) * (width - len(nums_a)), rel_a, pre_a)
    key_b = (nums_b + (0,) * (width - len(nums_b)), rel_b, pre_b)
    if key_a < key_b:
        return -1
    if key_a > key_b:
        return 1
    return 0


def verify_and_install_lua_pack(
    manifest: dict[str, Any],
    *,
    fetch_file_fn: Callable[[str], bytes],
    public_key_b64: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    allow_downgrade: bool = False,
) -> dict[str, Any]:
    """Verify Ed25519 signature + every file's SHA-256 + anti-rollback version, then atomically activate."""
    if not verify_manifest_signature(manifest, public_key_b64):
        raise ValueError("OTA Lua pack Ed25519 signature verification failed")

    game_id = str(manifest.get("game_id") or "").strip()
    version = str(manifest.get("version") or "").strip()
    if not _GAME_ID_RE.match(game_id) or not _VERSION_RE.match(version):
        raise ValueError("Invalid game_id or version in OTA manifest")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("OTA manifest has no files")

    game_root = lua_packs_root(data_dir) / game_id
    active_ptr = game_root / "active.json"
    if not allow_downgrade and active_ptr.is_file():
        try:
            active_doc = json.loads(active_ptr.read_text(encoding="utf-8"))
            active_ver = str(active_doc.get("version") or "").strip()
        except Exception:  # noqa: BLE001
            active_ver = ""
        if active_ver and _compare_semver(version, active_ver) < 0:
            raise ValueError(
                f"OTA Lua pack downgrade rejected for {game_id}: {version} < active {active_ver}"
            )

    staging_dir = game_root / f".staging_{version}_{os.getpid()}"
    target_dir = game_root / version
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    verified_files: list[str] = []
    try:
        for item in raw_files:
            if not isinstance(item, dict):
                raise ValueError("Invalid file entry in OTA manifest")
            rel = _validate_lua_rel_path(str(item.get("path") or ""))
            expected_sha = str(item.get("sha256") or "").strip().lower()
            if len(expected_sha) != 64:
                raise ValueError(f"Invalid SHA-256 in manifest for '{rel}'")

            content = fetch_file_fn(rel)
            if not isinstance(content, (bytes, bytearray)):
                raise ValueError(f"Fetcher did not return bytes for '{rel}'")
            if len(content) > MAX_LUA_SCRIPT_BYTES:
                raise ValueError(f"Downloaded script '{rel}' exceeds size limit")

            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(f"SHA-256 mismatch for '{rel}' (expected {expected_sha[:12]}..., got {actual_sha[:12]}...)")

            dest = (staging_dir / rel).resolve()
            if staging_dir.resolve() not in dest.parents:
                raise ValueError(f"Resolved path escapes staging directory: '{rel}'")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(bytes(content))
            verified_files.append(rel)

        (staging_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        if target_dir.exists():
            shutil.rmtree(target_dir)
        staging_dir.replace(target_dir)

        active_ptr = game_root / "active.json"
        tmp_ptr = active_ptr.with_suffix(f".tmp_{os.getpid()}")
        tmp_ptr.write_text(
            json.dumps({"game_id": game_id, "version": version, "files": verified_files}, indent=2),
            encoding="utf-8",
        )
        tmp_ptr.replace(active_ptr)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return {
        "ok": True,
        "game_id": game_id,
        "version": version,
        "install_dir": str(target_dir),
        "files": verified_files,
    }


def resolve_lua_script(
    script_name: str,
    *,
    game_id: str = "sfiii3nr1",
    data_dir: Path = DEFAULT_DATA_DIR,
    bundled_lua_dir: Path | None = None,
) -> Path:
    """Resolve `script_name` from active verified OTA Lua pack if present, else bundled `lua/`."""
    clean_rel = _validate_lua_rel_path(script_name)
    active_ptr = lua_packs_root(data_dir) / game_id / "active.json"
    if active_ptr.is_file():
        try:
            doc = json.loads(active_ptr.read_text(encoding="utf-8"))
            ver = str(doc.get("version") or "").strip()
            if ver and _VERSION_RE.match(ver):
                cand = (lua_packs_root(data_dir) / game_id / ver / clean_rel).resolve()
                pack_root = (lua_packs_root(data_dir) / game_id / ver).resolve()
                if pack_root in cand.parents and cand.is_file():
                    return cand
        except Exception:  # noqa: BLE001
            pass
    base = bundled_lua_dir or (REPO_ROOT / "lua")
    return (base / clean_rel).resolve()
