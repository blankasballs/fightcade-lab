# Fightcade Lab

**A free, open-source Replay Studio, Matchup Notebook, and Instant Replay Takeover trainer for Fightcade** — starting with *Street Fighter III: 3rd Strike*.

📦 **[Download Windows Companion](https://github.com/blankasballs/fightcade-lab/releases)** · 🌐 **https://fightcadelab.com** *(coming soon)* · 💬 **[Feedback & Issues](https://github.com/blankasballs/fightcade-lab/issues)** · ⚖️ **[AGPL-3.0](./LICENSE)**

> **Play** → **Review with hitboxes & inputs** → **Track Focus Goals & Playlists** → **Jump straight into Takeover**

---

## Why Fightcade Lab?

- **Frame-Accurate Replay Studio:** Scrub every round at 60fps on desktop or your phone (over LAN/VPN) with an interactive HP timeline, instant P1/P2 perspective flip, and toggleable hitboxes, inputs, sticks, and gauges.
- **Instant Replay Takeover:** Click any moment to drop straight into FBNeo at that exact frame — with slow-mo, one-button retry, and a Training Sandbox to test answers on the spot.
- **Zero-Friction Capture:** Tap `Select` during live matches to bookmark tough spots; your sets are captured silently in the background the moment your match ends.
- **Focus Goals, Notes & Playlists:** Keep a searchable matchup notebook, pin up to 3 active improvement goals, and group moments into Playlists you can watch back-to-back or drill in Takeover.

---

## Safety, Privacy & Fightcade Stewardship

- **Never touches live netplay:** Zero DLL injection, zero live memory hooks, never modifies your stock Fightcade install, and never asks for your Fightcade password. All processing runs strictly **post-match** on finished replays.
- **100% local & respectful of Fightcade:** All replays, notes, and savestates stay on your PC. Single-pass capture streams each match at most once (`concurrency = 1`, built-in cooldowns and rate-limit backoff). **No arcade ROMs are bundled.**
- **Transparent open-source builds:** Released via public GitHub Actions builds with SHA-256 checksums under `AGPL-3.0-or-later`, cleanly separated from optional external tools ([`fightcade-fbneo-fast`](https://github.com/blankasballs/fightcade-fbneo-fast), `ffmpeg`, and Grouflon's `3rd_training_lua`).

---

## Getting Started

### For Players (Windows Installer — No Terminal Required)

> **Requirements:** Windows 10/11 (64-bit) with Fightcade and your own `sfiii3nr1` (*3rd Strike*) ROM already installed.

1. Download **`FightcadeLabSetup.exe`** from **[GitHub Releases](https://github.com/blankasballs/fightcade-lab/releases)** and run the installer.
2. Follow the **First-Run Wizard** — it auto-detects your Fightcade folder and ROMs, and optionally downloads verified copies of `fcadefbneo_fast.exe` (for fast invisible capture & slow-mo Takeover) and `ffmpeg.exe`.
3. Click **`Process My Latest Match`** to open your most recent Fightcade set in the Replay Studio right away.
4. *(Optional)* Install [Grouflon's `3rd_training_lua`](https://github.com/Grouflon/3rd_training_lua) in your Fightcade FBNeo folder to unlock Hitbox/Gauge overlays and Sandbox Training Mode.

<details>
<summary><strong>Quick Controls Reference (In-Game & Replay Takeover)</strong></summary>

- **During live Fightcade matches:** Tap **`Select` (`Coin`)** anytime a sequence gives you trouble — Fightcade records it natively into the replay stream, and Fightcade Lab flags it automatically after the set.
- **During Replay Takeover:**
  - **`Select` (`Coin`)** or **`Alt+1`** — Instant 1-frame retry
  - **`Start`** — Open in-game Takeover menu (Game Speed `25%–125%`, Countdown delay, Sandbox Training Mode, P1/P2 swap)
  - **`Alt+2`** — Toggle Takeover vs. Watch Original
  - **`Alt+3`** — Toggle Auto-Loop
  - **`Alt+4`** — Swap P1 / P2 control side
  - **`Alt+5`** — Toggle Sandbox Training Mode *(requires Grouflon `3rd_training_lua`)*
  - **`Alt+6` / `Alt+7` / `Alt+8`** — Decrease / increase / toggle slow-mo speed

</details>

---

### For Developers & Contributors

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[indexing,dev]"

# Start Fightcade Lab locally on http://127.0.0.1:8765/ (zero firewall prompt)
.\scripts\serve_reports.ps1

# Optional: allow phone / tablet access over LAN or Tailscale (0.0.0.0:8765)
.\scripts\serve_reports.ps1 -Lan

# Run test suite
.\.venv\Scripts\python.exe -m pytest -q
```

*(Optional)* For local BYO-key Gemini AI Match Breakdowns, create a gitignored `.env` in the repo root with `GEMINI_API_KEY=your_key_here`.

---

## Repository Overview

```text
src/fightcade_analysis/
  web/              Zero-build responsive UI (Home Workbench, 2-Col Studio, Notes, Playlists, Insights)
  server.py         Local HTTP server (206 Range streaming, API, auto-reload)
  tray_app.py       Windows System Tray app & First-Run onboarding wizard
  round_renderer.py Single-pass & offline per-round MP4 + overlay + .fs drill baker
  takeover.py       Instant <1s Replay Takeover launcher
  focus_goals.py    Pinned Focus Goals (max 3 active + graduated history)
  playlists.py      Watch Mode & Train Mode playlists
  annotations.py    Moment notes, ranges & set takeaways
  notes_hub.py      Centralized searchable matchup notebook
  perspective.py    Single P1/P2 perspective projection boundary
lua/                Lua 5.1 scripts for FBNeo capture, overlays & Replay Takeover
packaging/          Windows installer build pipeline (Nuitka + Inno Setup)
tests/              Automated Python unit & integration tests (pytest)
```

---

## Roadmap

- **Web Clip Sharing & Feedback (https://fightcadelab.com):** Share short moments and playlists with playable Discord previews and frame-pinned coaching replies.
- **Multi-Game Support:** Expanding beyond *3rd Strike* to *KOF 2002*, *Vampire Savior*, *Super Turbo*, and other Fightcade FBNeo classics.
- **Pro-Match Comparison ("Find Pro Answers"):** Exploring Fightcade-friendly ways to compare tough sequences against high-level play once community demand aligns.

---

## License & Credits

- Licensed under **GNU AGPL-3.0-or-later** — see [LICENSE](./LICENSE).
- See [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md) for full licensing boundaries and attributions.
- Built with deep appreciation for the **Fightcade** team, **FinalBurn Neo**, **Grouflon** ([`3rd_training_lua`](https://github.com/Grouflon/3rd_training_lua)), and **peon2 / contributors** ([`fbneo-training-mode`](https://github.com/peon2/fbneo-training-mode)).
