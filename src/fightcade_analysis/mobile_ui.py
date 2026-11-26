"""Fightcade Lab Unified Web App wrapper (`/`, aliased at `/review` and `/mobile`).

Reads static frontend assets from `src/fightcade_analysis/web/` (zero build step, zero CDN deps)
and injects lightweight bootstrap config (`__FC_BOOTSTRAP__`).
"""

from __future__ import annotations

import json
from pathlib import Path

from fightcade_analysis.annotations import PRESET_TAGS

WEB_ASSETS_DIR = Path(__file__).resolve().parent / "web"


def build_pwa_manifest_json() -> str:
    return json.dumps(
        {
            "name": "Fightcade Lab",
            "short_name": "Fightcade Lab",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0b0f19",
            "theme_color": "#0b0f19",
            "description": "Review 3rd Strike replays, track Focus Goals, build Study/Train Playlists, and launch instant Takeover drills.",
        },
        indent=2,
    )


def build_mobile_app_html(
    *,
    default_username: str = "",
    initial_route: str | None = None,
) -> str:
    """Return Fightcade Lab HTML shell with server-injected bootstrap globals."""
    index_path = WEB_ASSETS_DIR / "index.html"
    raw_html = index_path.read_text(encoding="utf-8")
    bootstrap = json.dumps(
        {
            "mode": "local_companion",
            "apiBase": "",
            "companionBase": "",
            "defaultUsername": str(default_username or ""),
            "presetTags": list(PRESET_TAGS),
            "githubRepoUrl": "https://github.com/blankasballs/fightcade-lab",
            "dmcaContactEmail": "dmca@fightcadelab.com",
            "initialRoute": initial_route,
        },
        ensure_ascii=False,
    )
    # Prevent accidental `</script>` breakout if username ever contains it
    bootstrap_safe = bootstrap.replace("</", "<\\/")
    return raw_html.replace("__FC_BOOTSTRAP_JSON__", bootstrap_safe)
