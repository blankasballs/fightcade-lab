"""Query Fightcade's public replay search API (`searchquarks`) and resolve stream targets.

Supports stratified / player-anchored sampling:
  - `--username` (repeatable) or `--players-file` for player-anchored high-rank pulls
  - `--min-rank` / `--min-any-rank` for skill filtering (0=unranked, 1=E .. 6=S)
  - `--min-duration` / `--max-duration` to skip aborted disconnects or marathon sets
  - `--max-per-pair` to prevent a single player pair from dominating a corpus
  - `--merge` to incrementally combine queries while preserving cached `stream_target`s
  - Polite request pacing & exponential backoff on HTTP 429/5xx
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Callable

import requests

API_URLS = (
    "https://web.fightcade.com/api/",
    "https://www.fightcade.com/api/",
)
REPLAY_REDIRECT_BASE = "https://replay.fightcade.com"
COMPANION_VERSION = "0.1"
DEFAULT_UA = f"FightcadeLab-Companion/{COMPANION_VERSION} (+https://fightcadelab.com)"
# Strict exponential backoff ladder: 30s -> 2m -> 10m -> pause (FightcadeRateLimitError)
DEFAULT_BACKOFF_SCHEDULE: tuple[float, ...] = (30.0, 120.0, 600.0)
_BACKOFF_STATUS_CODES = frozenset({403, 429})
_FCADE_STREAM_RE = re.compile(
    r"^fcade://stream/(?P<emulator>[^/]+)/(?P<gameid>[^/]+)/(?P<quark_ver>[^,]+),(?P<port>\d+)$"
)


class FightcadeRateLimitError(RuntimeError):
    """Raised when Fightcade/Cloudflare returns repeated 429/403/5xx and backoff pauses requests."""

    def __init__(self, message: str, *, status_code: int = 429, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.retry_after = retry_after


def parse_retry_after(header_val: str | None) -> float | None:
    """Parse HTTP `Retry-After` header (integer/float seconds or RFC 7231 HTTP-date)."""
    if not header_val:
        return None
    raw = str(header_val).strip()
    if not raw:
        return None
    try:
        secs = float(raw)
        return max(0.0, secs)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta)
    except Exception:  # noqa: BLE001
        pass
    return None


def _compute_backoff_wait(
    attempt: int,
    *,
    retry_after: str | None = None,
    schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    idx = min(attempt, max(0, len(schedule) - 1))
    base_wait = float(schedule[idx]) if schedule else 30.0
    ra = parse_retry_after(retry_after)
    if ra is not None:
        return max(ra, base_wait)
    return base_wait + max(0.0, float(jitter_fn(0.0, min(5.0, base_wait * 0.1))))


def _is_backoff_status(status_code: int) -> bool:
    return status_code in _BACKOFF_STATUS_CODES or (500 <= status_code < 600)


def _request_with_backoff(
    session: requests.Session,
    method: str,
    url: str,
    *,
    max_retries: int | None = None,
    backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[float, float], float] = random.uniform,
    **kwargs: Any,
) -> requests.Response:
    """Execute HTTP request with strict `30s -> 2m -> 10m -> pause` backoff on 429/403/5xx."""
    retries = len(backoff_schedule) if max_retries is None else max(0, int(max_retries))
    last_exc: Exception | None = None
    last_status: int | None = None
    last_ra: float | None = None

    for attempt in range(retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if _is_backoff_status(resp.status_code):
                last_status = int(resp.status_code)
                last_ra = parse_retry_after(resp.headers.get("Retry-After"))
                if attempt < retries:
                    wait = _compute_backoff_wait(
                        attempt,
                        retry_after=resp.headers.get("Retry-After"),
                        schedule=backoff_schedule,
                        jitter_fn=jitter_fn,
                    )
                    sleep_fn(wait)
                    continue
                raise FightcadeRateLimitError(
                    f"Fightcade request paused after HTTP {last_status} across {retries} backoff retries ({url})",
                    status_code=last_status,
                    retry_after=last_ra,
                )
            return resp
        except FightcadeRateLimitError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                wait = _compute_backoff_wait(
                    attempt,
                    schedule=backoff_schedule,
                    jitter_fn=jitter_fn,
                )
                sleep_fn(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise FightcadeRateLimitError(
        f"Fightcade request paused after {retries} retries: {url}",
        status_code=last_status or 429,
        retry_after=last_ra,
    )


def parse_fcade_stream_location(location: str) -> str:
    """Convert a `fcade://stream/<emu>/<game>/<quarkid>.<ver>,<port>` URI into a `quark:stream,...` arg."""
    match = _FCADE_STREAM_RE.match(location.strip())
    if not match:
        raise ValueError(f"Unexpected Fightcade replay redirect Location: {location!r}")
    gameid = match.group("gameid")
    quark_ver = match.group("quark_ver")
    port = match.group("port")
    return f"quark:stream,{gameid},{quark_ver},{port}"


def resolve_stream_target(
    gameid: str,
    quarkid: str,
    *,
    emulator: str = "fbneo",
    session: requests.Session | None = None,
    timeout: float = 15.0,
    max_retries: int | None = None,
    backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Resolve `https://replay.fightcade.com/<emu>/<gameid>/<quarkid>` 301 redirect to `quark:stream,...`."""
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_UA)
    url = f"{REPLAY_REDIRECT_BASE}/{emulator}/{gameid}/{quarkid}"
    resp = _request_with_backoff(
        sess,
        "HEAD",
        url,
        allow_redirects=False,
        timeout=timeout,
        max_retries=max_retries,
        backoff_schedule=backoff_schedule,
        sleep_fn=sleep_fn,
    )
    location = resp.headers.get("Location")
    if resp.status_code not in (301, 302, 303, 307, 308) or not location:
        raise RuntimeError(
            f"Expected 30x redirect with Location header for {url}, got {resp.status_code} ({location!r})"
        )
    return parse_fcade_stream_location(location)


def _pair_key(item: dict[str, Any]) -> tuple[str, str]:
    players = item.get("players", [])
    names = sorted((p.get("name") or "").lower() for p in players[:2])
    while len(names) < 2:
        names.append("")
    return (names[0], names[1])


def filter_quarks(
    items: list[dict[str, Any]],
    *,
    ranked_only: bool = False,
    min_rank: int = 0,
    min_any_rank: int = 0,
    min_duration: float = 0.0,
    max_duration: float = 0.0,
    max_per_pair: int = 0,
    seen_quarkids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter raw Fightcade quark objects for skill, duration, and player-pair diversity."""
    out: list[dict[str, Any]] = []
    pair_counts: Counter[tuple[str, str]] = Counter()
    seen = set(seen_quarkids) if seen_quarkids else set()

    for item in items:
        qid = item.get("quarkid")
        if not qid or qid in seen:
            continue

        if ranked_only and not item.get("ranked"):
            continue

        dur = float(item.get("duration") or 0.0)
        if min_duration > 0 and dur < min_duration:
            continue
        if max_duration > 0 and dur > max_duration:
            continue

        players = item.get("players", [])
        ranks = [int(p.get("rank") or 0) for p in players[:2]]
        if len(ranks) < 2:
            ranks.extend([0] * (2 - len(ranks)))

        if min_rank > 0 and min(ranks) < min_rank:
            continue
        if min_any_rank > 0 and max(ranks) < min_any_rank:
            continue

        pk = _pair_key(item)
        if max_per_pair > 0 and pair_counts[pk] >= max_per_pair:
            continue

        pair_counts[pk] += 1
        seen.add(qid)
        out.append(item)

    return out


def fetch_quarks(
    gameid: str = "sfiii3nr1",
    limit: int = 50,
    best: bool = True,
    username: str | None = None,
    ranked_only: bool = False,
    min_rank: int = 0,
    min_any_rank: int = 0,
    min_duration: float = 0.0,
    max_duration: float = 0.0,
    max_per_pair: int = 0,
    offset: int = 0,
    max_pages: int = 10,
    page_delay: float = 1.0,
    timeout: float = 15.0,
    max_retries: int | None = None,
    session: requests.Session | None = None,
    stop_at_known_ids: set[str] | None = None,
    backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SCHEDULE,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch filtered replay metadata objects from Fightcade's searchquarks endpoint.

    When `stop_at_known_ids` is provided, pagination halts immediately upon reaching any
    already-cached `quarkid` (incremental sync — avoids redundant page fetches).
    """
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    known_ids: set[str] = {str(x) for x in stop_at_known_ids} if stop_at_known_ids else set()
    pair_counts: Counter[tuple[str, str]] = Counter()
    cur_offset = offset

    sess = session or requests.Session()
    sess.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )

    for page_idx in range(max_pages):
        if len(results) >= limit:
            break
        if page_idx > 0 and page_delay > 0:
            sleep_fn(page_delay)

        batch_limit = 50  # always request full page when filtering to avoid extra round-trips
        payload: dict[str, Any] = {
            "req": "searchquarks",
            "gameid": gameid,
            "offset": cur_offset,
            "limit": batch_limit,
        }
        if username:
            payload["username"] = username
        elif best:
            payload["best"] = True

        resp_json: dict[str, Any] | None = None
        last_err: Exception | None = None
        for url in API_URLS:
            try:
                r = _request_with_backoff(
                    sess,
                    "POST",
                    url,
                    json=payload,
                    timeout=timeout,
                    max_retries=max_retries,
                    backoff_schedule=backoff_schedule,
                    sleep_fn=sleep_fn,
                )
                r.raise_for_status()
                resp_json = r.json()
                break
            except FightcadeRateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc

        if resp_json is None:
            if results:
                print(f"WARNING: Stopping pagination at offset={cur_offset} after error ({last_err}); returning {len(results)} results.", file=sys.stderr)
                break
            raise RuntimeError(f"Failed to query Fightcade API: {last_err}")

        batch = resp_json.get("results", {}).get("results")
        if batch is None:
            batch = resp_json.get("results") if isinstance(resp_json.get("results"), list) else []

        if not batch:
            break

        # Incremental stop-at-known-quark_id check before filtering so we stop as soon as we hit cached history
        hit_known = False
        unseen_batch: list[dict[str, Any]] = []
        if known_ids:
            for item in batch:
                qid = str(item.get("quarkid") or "")
                if qid and qid in known_ids:
                    hit_known = True
                    break
                unseen_batch.append(item)
        else:
            unseen_batch = batch

        filtered = filter_quarks(
            unseen_batch,
            ranked_only=ranked_only,
            min_rank=min_rank,
            min_any_rank=min_any_rank,
            min_duration=min_duration,
            max_duration=max_duration,
            max_per_pair=0,  # enforce pair cap across pages below
            seen_quarkids=seen_ids,
        )
        for item in filtered:
            pk = _pair_key(item)
            if max_per_pair > 0 and pair_counts[pk] >= max_per_pair:
                continue
            pair_counts[pk] += 1
            seen_ids.add(item["quarkid"])
            results.append(item)
            if len(results) >= limit:
                break

        if hit_known:
            break

        cur_offset += len(batch)
        if len(batch) < batch_limit:
            break

    return results


def summarize_quark(
    item: dict[str, Any],
    stream_target: str | None = None,
    source_tag: str | None = None,
) -> dict[str, Any]:
    players = item.get("players", [])
    p1 = players[0] if len(players) > 0 else {}
    p2 = players[1] if len(players) > 1 else {}
    return {
        "quarkid": item.get("quarkid"),
        "gameid": item.get("gameid"),
        "emulator": item.get("emulator", "fbneo"),
        "date": item.get("date"),
        "duration": item.get("duration"),
        "num_matches": item.get("num_matches"),
        "ranked": item.get("ranked"),
        "p1_name": p1.get("name"),
        "p1_rank": p1.get("rank"),
        "p1_score": p1.get("score"),
        "p2_name": p2.get("name"),
        "p2_rank": p2.get("rank"),
        "p2_score": p2.get("score"),
        "stream_target": stream_target,
        "source_tag": source_tag,
    }


def merge_manifest_records(
    existing: dict[str, Any] | None,
    new_raw: list[dict[str, Any]],
    new_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge new quark records into an existing manifest JSON without losing cached stream_targets."""
    raw_by_id: dict[str, dict[str, Any]] = {}
    sum_by_id: dict[str, dict[str, Any]] = {}

    if existing:
        for r in existing.get("raw", []):
            if r.get("quarkid"):
                raw_by_id[r["quarkid"]] = r
        for s in existing.get("summaries", []):
            if s.get("quarkid"):
                sum_by_id[s["quarkid"]] = s

    for r in new_raw:
        qid = r.get("quarkid")
        if qid:
            raw_by_id[qid] = r

    for s in new_summaries:
        qid = s.get("quarkid")
        if not qid:
            continue
        prev = sum_by_id.get(qid)
        if prev and prev.get("stream_target") and not s.get("stream_target"):
            s = dict(s)
            s["stream_target"] = prev["stream_target"]
        sum_by_id[qid] = s

    return {
        "raw": list(raw_by_id.values()),
        "summaries": list(sum_by_id.values()),
    }


def _load_existing_manifest(out_path: Path | None) -> dict[str, Any] | None:
    if not out_path or not out_path.exists():
        return None
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Fightcade replay metadata with stratified/player-anchored filters.")
    parser.add_argument("--gameid", default="sfiii3nr1", help="Fightcade game ID (default: sfiii3nr1)")
    parser.add_argument("--limit", type=int, default=15, help="Replays to fetch (per username if --username is used)")
    parser.add_argument("--offset", type=int, default=0, help="Starting pagination offset")
    parser.add_argument("--recent", action="store_true", help="Fetch recent instead of best/high-rank")
    parser.add_argument("--username", nargs="+", default=None, help="One or more Fightcade usernames to query")
    parser.add_argument("--players-file", type=Path, default=None, help="JSON file listing usernames (list or {tag: [names]})")
    parser.add_argument("--ranked-only", action="store_true", help="Filter to ranked matches only")
    parser.add_argument("--min-rank", type=int, default=0, help="Minimum rank for BOTH players (0..6; 4=B, 5=A, 6=S)")
    parser.add_argument("--min-any-rank", type=int, default=0, help="Minimum rank for AT LEAST ONE player (0..6)")
    parser.add_argument("--min-duration", type=float, default=60.0, help="Minimum match duration in seconds (default: 60)")
    parser.add_argument("--max-duration", type=float, default=0.0, help="Maximum match duration in seconds (0 = unlimited)")
    parser.add_argument("--max-per-pair", type=int, default=0, help="Max replays per unique player pair (0 = unlimited)")
    parser.add_argument("--max-pages", type=int, default=8, help="Max API pages to scan per query (default: 8)")
    parser.add_argument("--page-delay", type=float, default=1.0, help="Seconds between searchquarks pages (default: 1.0)")
    parser.add_argument("--resolve", action="store_true", help="Resolve each quarkid via replay.fightcade.com redirect")
    parser.add_argument("--resolve-delay", type=float, default=0.5, help="Seconds between redirect HEAD requests (default: 0.5)")
    parser.add_argument("--resolve-one", type=str, default=None, help="Resolve a single quarkid and print its quark:stream target")
    parser.add_argument("--merge", action="store_true", help="Merge into existing --out manifest instead of overwriting")
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON output path")
    args = parser.parse_args(argv)

    if args.resolve_one:
        print(resolve_stream_target(args.gameid, args.resolve_one))
        return 0

    existing = _load_existing_manifest(args.out)
    cached_targets = {
        s["quarkid"]: s["stream_target"]
        for s in (existing or {}).get("summaries", [])
        if s.get("quarkid") and s.get("stream_target")
    }

    # Assemble (username, source_tag) query list
    queries: list[tuple[str | None, str]] = []
    if args.username:
        for u in args.username:
            queries.append((u, f"user:{u}"))
    if args.players_file:
        pf = json.loads(args.players_file.read_text(encoding="utf-8"))
        if isinstance(pf, list):
            for u in pf:
                queries.append((str(u), f"user:{u}"))
        elif isinstance(pf, dict):
            for tag, names in pf.items():
                for u in names:
                    queries.append((str(u), f"{tag}:{u}"))
    if not queries:
        queries.append((None, "recent" if args.recent else "best"))

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_UA})

    all_raw: list[dict[str, Any]] = []
    all_summaries: list[dict[str, Any]] = []
    global_seen: set[str] = set()
    resolved_requests = 0

    for q_idx, (uname, tag) in enumerate(queries):
        if q_idx > 0 and args.page_delay > 0:
            time.sleep(args.page_delay)

        try:
            batch = fetch_quarks(
                gameid=args.gameid,
                limit=args.limit,
                best=not args.recent,
                username=uname,
                ranked_only=args.ranked_only,
                min_rank=args.min_rank,
                min_any_rank=args.min_any_rank,
                min_duration=args.min_duration,
                max_duration=args.max_duration,
                max_per_pair=args.max_per_pair,
                offset=args.offset,
                max_pages=args.max_pages,
                page_delay=args.page_delay,
                session=session,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: Query '{tag}' failed ({exc}); continuing with remaining queries.", file=sys.stderr)
            continue

        for item in batch:
            qid = item.get("quarkid")
            if not qid or qid in global_seen:
                continue
            global_seen.add(qid)
            all_raw.append(item)

            target = cached_targets.get(qid)
            if args.resolve and not target:
                if resolved_requests > 0 and args.resolve_delay > 0:
                    time.sleep(args.resolve_delay)
                try:
                    target = resolve_stream_target(
                        item.get("gameid", args.gameid),
                        qid,
                        emulator=item.get("emulator", "fbneo"),
                        session=session,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"WARNING: Failed to resolve {qid}: {exc}", file=sys.stderr)
                    target = None
                resolved_requests += 1
            all_summaries.append(summarize_quark(item, stream_target=target, source_tag=tag))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        final_doc = (
            merge_manifest_records(existing, all_raw, all_summaries)
            if args.merge
            else {"raw": all_raw, "summaries": all_summaries}
        )
        args.out.write_text(json.dumps(final_doc, indent=2), encoding="utf-8")
        print(f"Saved {len(final_doc['summaries'])} total replays to {args.out} ({len(all_summaries)} from this run)")

    for i, s in enumerate(all_summaries[:15], 1):
        suffix = f" | {s['stream_target']}" if s.get("stream_target") else ""
        print(
            f"{i:2d}. {s['quarkid']} | {s['p1_name']} (r{s['p1_rank']}) vs "
            f"{s['p2_name']} (r{s['p2_rank']}) | dur={s['duration']}s | ranked={s['ranked']}{suffix}"
        )

    if not all_summaries:
        print("No replays matched filters.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
