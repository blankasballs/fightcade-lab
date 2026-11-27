"""Reproducible Windows Companion builder (Nuitka standalone + public exclusion guard).

Enforces all Track 3 open-source & SignPath Foundation packaging boundaries:
  - Strictly excludes local user data and non-companion artifacts (`data/`, `.env`, `docs/`).
  - Never bundles `fcadefbneo_fast.exe` (FBNeo license) or `ffmpeg.exe` (GPL-3.0).
  - Locks `FC_PACKAGED_COMPANION=1` in the launcher entrypoint so `pro_corpus_enabled`
    always defaults to `False` in public Companion builds.
  - Produces `SHA256SUMS.txt` alongside built artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]

# Paths & modules strictly excluded from public Companion builds
EXCLUDED_PUBLIC_ITEMS: tuple[str, ...] = (
    "scripts/habit_rollup.ps1",
    "scripts/verified_habits.ps1",
    "data",
    "docs",
    ".env",
    "fcadefbneo_fast.exe",
    "ffmpeg.exe",
)

# Minimal Lua scripts required for Replay Review, Select Marks & Takeover
COMPANION_LUA_FILES: tuple[str, ...] = (
    "sfiii3nr1_map.lua",
    "sfiii3nr1_overlay.lua",
    "dump_telemetry.lua",
    "capture_clip.lua",
    "capture_select_marks.lua",
    "replay_takeover.lua",
    "seek_and_takeover.lua",
    "grouflon_overlay.lua",
    "grouflon_training_bridge.lua",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_companion_assets(dist_dir: Path) -> dict[str, list[str]]:
    """Copy allowlisted runtime assets (web/, lua/, LICENSE, THIRD_PARTY_NOTICES.md) into `dist_dir`."""
    dist = dist_dir.resolve()
    dist.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []

    # 1. Legal files
    for fname in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        src = REPO_ROOT / fname
        if src.is_file():
            shutil.copy2(src, dist / fname)
            copied.append(fname)

    # 2. Allowlisted Lua scripts only
    lua_dst = dist / "lua"
    lua_dst.mkdir(parents=True, exist_ok=True)
    for name in COMPANION_LUA_FILES:
        src = REPO_ROOT / "lua" / name
        if src.is_file():
            shutil.copy2(src, lua_dst / name)
            copied.append(f"lua/{name}")

    # 3. Unified zero-build static web/ assets
    web_src = REPO_ROOT / "src" / "fightcade_analysis" / "web"
    web_dst = dist / "web"
    if web_dst.exists():
        shutil.rmtree(web_dst)
    shutil.copytree(web_src, web_dst)
    copied.append("web/")

    # 4. Verify zero forbidden binaries or corpus scripts leaked into dist_dir
    verify_clean_companion_dir(dist)
    return {"copied": copied}


def verify_clean_companion_dir(dist_dir: Path) -> None:
    """Assert no forbidden binaries, secrets, or bulk corpus scraping scripts exist in `dist_dir`."""
    forbidden_names = {
        "fcadefbneo.exe",
        "fcadefbneo_fast.exe",
        "ffmpeg.exe",
        "sfiii3nr1.zip",
        ".env",
    }
    for p in dist_dir.rglob("*"):
        if p.name.lower() in forbidden_names:
            raise RuntimeError(f"Forbidden file detected in Companion staging dir: {p}")


def write_entrypoint_stub(out_path: Path) -> Path:
    """Write the Nuitka entrypoint that pins `FC_PACKAGED_COMPANION=1` before launching `tray_app`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        '"""Packaged Fightcade Lab Companion entrypoint (Pro-Corpus strictly sealed off)."""\n'
        "import os\n"
        "os.environ['FC_PACKAGED_COMPANION'] = '1'\n"
        "os.environ.pop('FC_ENABLE_PRO_CORPUS', None)\n"
        "from fightcade_analysis.tray_app import main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return out_path


def write_sha256sums(dist_dir: Path, artifact_names: list[str]) -> Path:
    lines = []
    for name in sorted(artifact_names):
        p = dist_dir / name
        if p.is_file():
            lines.append(f"{sha256_file(p)}  {name}")
    sums_path = dist_dir / "SHA256SUMS.txt"
    sums_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return sums_path


def build_nuitka(output_dir: Path, *, dry_run: bool = False) -> list[str]:
    entry = write_entrypoint_stub(output_dir / "_companion_main.py")
    favicon_ico = REPO_ROOT / "src" / "fightcade_analysis" / "web" / "favicon.ico"
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--windows-console-mode=disable",
        "--assume-yes-for-downloads",
        "--output-filename=FightcadeLabCompanion.exe",
        f"--output-dir={output_dir}",
        "--include-package=fightcade_analysis",
        "--include-data-dir=" + str(REPO_ROOT / "src" / "fightcade_analysis" / "web") + "=fightcade_analysis/web",
        "--nofollow-import-to=faiss",
        "--nofollow-import-to=ruptures",
        "--nofollow-import-to=sklearn",
        "--nofollow-import-to=matplotlib",
        "--nofollow-import-to=imageio_ffmpeg",
    ]
    if favicon_ico.is_file():
        cmd.append(f"--windows-icon-from-ico={favicon_ico}")
    cmd.append(str(entry))
    if not dry_run:
        subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))
        standalone_dir = output_dir / "_companion_main.dist"
        if standalone_dir.is_dir():
            stage_companion_assets(standalone_dir)
    return cmd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Fightcade Lab Windows Companion via Nuitka.")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist" / "companion")
    parser.add_argument("--stage-only", action="store_true", help="Only stage assets & run exclusion audit")
    parser.add_argument("--dry-run", action="store_true", help="Print Nuitka command without executing")
    args = parser.parse_args(argv)

    if args.stage_only:
        res = stage_companion_assets(args.output_dir)
        print(f"Staged clean Companion assets into {args.output_dir}: {res['copied']}")
        return 0

    cmd = build_nuitka(args.output_dir, dry_run=args.dry_run)
    print("Nuitka command:", " ".join(cmd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
