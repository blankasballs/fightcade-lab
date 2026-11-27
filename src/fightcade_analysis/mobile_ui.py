"""Fightcade Lab Unified Web App wrapper (`/`, aliased at `/review` and `/mobile`).

Reads static frontend assets from `src/fightcade_analysis/web/` (zero build step, zero CDN deps)
and injects lightweight bootstrap config (`__FC_BOOTSTRAP__`).
"""

from __future__ import annotations

import json
from pathlib import Path

from fightcade_analysis.annotations import PRESET_TAGS
from fightcade_analysis.env_config import REPO_ROOT


def resolve_web_assets_dir() -> Path:
    """Resolve static web assets in both source checkouts and Nuitka frozen installs."""
    local_web = Path(__file__).resolve().parent / "web"
    if (local_web / "index.html").is_file():
        return local_web
    app_web = REPO_ROOT / "web"
    if (app_web / "index.html").is_file():
        return app_web
    return local_web


WEB_ASSETS_DIR = resolve_web_assets_dir()


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
            "icons": [
                {
                    "src": "/assets/img/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/assets/img/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
            ],
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
