"""Multimodal Gemini tactical interaction summarizer for Stage 4 coaching reports.

Sends the rendered 6-second MP4 clips (Your Choice + top Pro Alternatives) alongside grounded
RAM telemetry metadata directly to the Gemini REST API (`generativelanguage.googleapis.com`)
and caches concise structured JSON breakdowns locally to avoid redundant API calls.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
DEFAULT_MODEL_CHAIN: tuple[str, ...] = (
    "gemini-3-flash-preview",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
)
PROMPT_VERSION = "v1"


def load_gemini_api_key(env_path: Path = DEFAULT_ENV_PATH) -> str | None:
    """Resolve GEMINI_API_KEY from environment or local `.env` file."""
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        return env_val.strip("\"'")

    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "GEMINI_API_KEY":
                cleaned = v.strip().strip("\"'")
                if cleaned:
                    return cleaned
    return None


def _clip_fingerprint(path: Path | None) -> str:
    if path is None or not path.exists():
        return "missing"
    return path.name


def _cache_key(moment: dict[str, Any], clip_paths: dict[str, Path | None]) -> str:
    payload = {
        "v": PROMPT_VERSION,
        "qid": moment.get("quark_id"),
        "p": moment.get("perspective"),
        "gf": moment.get("game_frame"),
        "label": moment.get("label"),
        "clips": {k: _clip_fingerprint(v) for k, v in sorted(clip_paths.items())},
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _encode_mp4_part(path: Path) -> dict[str, Any]:
    data_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "inline_data": {
            "mime_type": "video/mp4",
            "data": data_b64,
        }
    }


def _build_prompt_parts(
    moment: dict[str, Any],
    clip_paths: dict[str, Path | None],
    *,
    pre_seconds: float = 3.5,
    total_seconds: float = 6.0,
) -> list[dict[str, Any]]:
    """Construct multimodal Gemini parts interleaving clear clip labels + MP4 bytes + RAM telemetry."""
    parts: list[dict[str, Any]] = []

    p_side = f"P{moment['perspective']}"
    opp_side = "P2" if int(moment["perspective"]) == 1 else "P1"

    header = (
        "You are an expert Street Fighter III: 3rd Strike coach analyzing short 6-second replay clips.\n"
        f"Each clip is {total_seconds:.1f}s long. The yellow HUD banner '>>> DECISION POINT (t0) <<<' "
        f"appears around {pre_seconds:.1f}s into each clip.\n"
        "Use BOTH the video frames (exact moves, jumps, parries, throws, fireballs, supers) and the "
        "RAM telemetry facts below. Be concise, specific, and accurate—never invent moves you do not see.\n\n"
        "=== TARGET MOMENT (YOUR CHOICE) ===\n"
        f"- Player being coached: {p_side} {moment['self_char_name']} (SA{moment.get('self_sa', '?')})\n"
        f"- Opponent: {opp_side} {moment['opp_char_name']} (SA{moment.get('opp_sa', '?')})\n"
        f"- Detected Situation: {moment['label']}\n"
        f"- State at t0: spacing={moment['t0_dx_px']:.0f}px, self_corner={moment['t0_self_corner']:.2f}, "
        f"self_hp={moment['t0_self_hp']*100:.0f}%, opp_hp={moment['t0_opp_hp']*100:.0f}%\n"
        f"- User coarse input: {moment['user_action']} | Outcome={moment['actual_value']:+.2f} "
        f"(HP swing {moment['actual_hp_swing']:+.2f})\n"
        f"- Corpus expected value: {moment['ev_corpus']:+.2f} (Regret {moment['regret']:+.2f})\n"
    )
    parts.append({"text": header})

    user_mp4 = clip_paths.get("user")
    if user_mp4 and user_mp4.exists():
        parts.append({"text": "\n[CLIP: user — Target player's choice]\n"})
        parts.append(_encode_mp4_part(user_mp4))

    alt_idx = 0
    for opt in moment.get("options", [])[:2]:
        proofs = opt.get("proofs", [])
        if not proofs:
            continue
        alt_idx += 1
        pr = proofs[0]
        key = f"alt_{alt_idx}"
        alt_side = f"P{pr['perspective']}"
        alt_desc = (
            f"\n[CLIP: {key} — Pro Alternative #{alt_idx}]\n"
            f"- Action bucket: {opt['action_coarse']} (Option EV {opt['ev']:+.2f} across {opt['count']} similar spots, "
            f"clip similarity {pr.get('similarity_pct', '?')}%)\n"
            f"- Focus player in this clip: {alt_side} {pr['self_char_name']} vs {pr['opp_char_name']}\n"
            f"- Situation: {pr['label']} | Outcome={pr['outcome_value']:+.2f} (HP swing {pr['net_hp_swing']:+.2f})\n"
        )
        parts.append({"text": alt_desc})
        alt_mp4 = clip_paths.get(key)
        if alt_mp4 and alt_mp4.exists():
            parts.append(_encode_mp4_part(alt_mp4))

    instructions = (
        "\nReturn ONLY a JSON object with this exact schema:\n"
        "{\n"
        '  "situation_summary": "1-2 crisp sentences explaining the setup before t0, what the target player did at t0, and why it went badly.",\n'
        '  "alternative_takeaway": "1-2 crisp sentences explaining what the pro alternative(s) did differently and the concrete adjustment to drill in Replay Takeover.",\n'
        '  "clip_captions": {\n'
        '    "user": "One short play-by-play sentence for the user clip.",\n'
        '    "alt_1": "One short play-by-play sentence for alt_1 (omit if not provided).",\n'
        '    "alt_2": "One short play-by-play sentence for alt_2 (omit if not provided)."\n'
        "  }\n"
        "}\n"
    )
    parts.append({"text": instructions})
    return parts


def _call_gemini_json(
    parts: list[dict[str, Any]],
    *,
    api_key: str,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
    timeout_seconds: int = 90,
    degraded_models: set[str] | None = None,
    degraded_lock: threading.Lock | None = None,
) -> tuple[dict[str, Any], str]:
    """Call Gemini REST generateContent with JSON output and sticky model fallback on 503/429/404."""
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.25,
            "responseMimeType": "application/json",
        },
    }
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    # Try non-degraded models first, then fall back to degraded models only if all others failed
    if degraded_models is not None and degraded_lock is not None:
        with degraded_lock:
            skipped = set(degraded_models)
        ordered_models = [m for m in models if m not in skipped] + [m for m in models if m in skipped]
    else:
        ordered_models = list(models)

    last_err: Exception | None = None
    for model in ordered_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        max_attempts = 3 if model == ordered_models[0] else 2
        for attempt in range(max_attempts):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=timeout_seconds)
                if resp.status_code in (429, 503):
                    last_err = RuntimeError(f"{model} returned HTTP {resp.status_code}: {resp.text[:200]}")
                    if attempt + 1 < max_attempts:
                        time.sleep(2.0 * (2**attempt))
                        continue
                    break
                if resp.status_code >= 400:
                    last_err = RuntimeError(f"{model} returned HTTP {resp.status_code}: {resp.text[:300]}")
                    # Permanent model errors (e.g. 404 deprecated) stay skipped for the rest of this run
                    if resp.status_code in (400, 404) and degraded_models is not None and degraded_lock is not None:
                        with degraded_lock:
                            degraded_models.add(model)
                    break
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    raise ValueError("Gemini JSON response was not an object")
                return parsed, model
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < max_attempts:
                    time.sleep(1.5)
                    continue
                break

    raise RuntimeError(f"All Gemini models failed ({models}): {last_err}")


def summarize_report_moments(
    report: dict[str, Any],
    *,
    clips_dir: Path,
    cache_path: Path | None = None,
    max_moments: int = 5,
    api_key: str | None = None,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
    pre_seconds: float = 3.5,
    total_seconds: float = 6.0,
    max_workers: int = 2,
    force: bool = False,
) -> tuple[int, dict[str, int]]:
    """Attach cached multimodal Gemini interaction summaries to `report['moments']` in place (concurrently).

    Returns:
        (total_summarized_count, stats_dict)
    """
    resolved_key = api_key or load_gemini_api_key()
    if not resolved_key:
        print("WARNING: GEMINI_API_KEY not found in environment or .env; skipping AI summaries.")
        return 0, {"cached": 0, "api_calls": 0, "failed": 0}

    cpath = cache_path or (clips_dir.parent / "reports" / "gemini_summary_cache.json")
    cache = _load_cache(cpath)
    stats = {"cached": 0, "api_calls": 0, "failed": 0}
    pending: list[tuple[int, dict[str, Any], dict[str, Path | None], str, str]] = []

    for idx, moment in enumerate(report.get("moments", [])[:max_moments], 1):
        user_stem = f"{moment['quark_id']}_gf{int(moment['game_frame'])}"
        clip_paths: dict[str, Path | None] = {"user": clips_dir / f"{user_stem}.mp4"}

        alt_idx = 0
        for opt in moment.get("options", [])[:2]:
            proofs = opt.get("proofs", [])
            if proofs:
                alt_idx += 1
                pr = proofs[0]
                pr_stem = f"{pr['quark_id']}_gf{int(pr['game_frame'])}"
                clip_paths[f"alt_{alt_idx}"] = clips_dir / f"{pr_stem}.mp4"

        ckey = _cache_key(moment, clip_paths)
        if not force and ckey in cache:
            moment["ai_summary"] = cache[ckey]
            stats["cached"] += 1
        else:
            pending.append((idx, moment, clip_paths, ckey, user_stem))

    if pending:
        degraded_models: set[str] = set()
        degraded_lock = threading.Lock()

        def _worker(item: tuple[int, dict[str, Any], dict[str, Path | None], str, str]) -> tuple[int, dict[str, Any], str, dict[str, Any]]:
            idx, moment, clip_paths, ckey, _ = item
            parts = _build_prompt_parts(
                moment,
                clip_paths,
                pre_seconds=pre_seconds,
                total_seconds=total_seconds,
            )
            parsed, model_used = _call_gemini_json(
                parts,
                api_key=resolved_key,
                models=models,
                degraded_models=degraded_models,
                degraded_lock=degraded_lock,
            )
            entry = {
                "model": model_used,
                "situation_summary": str(parsed.get("situation_summary", "")).strip(),
                "alternative_takeaway": str(parsed.get("alternative_takeaway", "")).strip(),
                "clip_captions": parsed.get("clip_captions", {})
                if isinstance(parsed.get("clip_captions"), dict)
                else {},
            }
            return idx, moment, ckey, entry

        workers = max(1, min(max_workers, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_worker, item): item for item in pending}
            for fut in as_completed(futs):
                idx, _, _, _, user_stem = futs[fut]
                try:
                    done_idx, moment, ckey, entry = fut.result()
                    moment["ai_summary"] = entry
                    cache[ckey] = entry
                    stats["api_calls"] += 1
                    print(f"  [Gemini] Summarized moment #{done_idx} via {entry['model']}")
                except Exception as exc:  # noqa: BLE001
                    stats["failed"] += 1
                    print(f"WARNING: Gemini summary failed for moment #{idx} ({user_stem}): {exc}")

        _save_cache(cpath, cache)

    return stats["cached"] + stats["api_calls"], stats
