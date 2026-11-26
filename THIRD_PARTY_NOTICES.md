# Third-Party Notices & Licensing Boundaries

**Fightcade Lab** (`fightcade-analysis`) is licensed under **GNU AGPL-3.0-or-later** (see `LICENSE`).

This document clarifies our strict licensing boundaries and credits the open-source and community projects Fightcade Lab interacts with or depends upon.

---

## 1. Strictly Excluded / Never Bundled External Components

Fightcade Lab's repository and signed Windows Companion installer **never** bundle, vendor, or redistribute any of the following:

1. **Arcade ROMs / BIOS files:** Users supply their own legally owned arcade ROMs inside their existing local Fightcade installation. Fightcade Lab never hosts, distributes, or streams ROM data.
2. **Grouflon's `3rd_training_lua`** (*All Rights Reserved*):
   - Never vendored or modified in this repository.
   - Detected optionally at runtime in the user's own local Fightcade `fbneo` folder to provide optional hitbox overlays and Sandbox Training. Fightcade Lab degrades gracefully if absent.
3. **`fcadefbneo_fast.exe` (FBNeo Engine Fork — FBNeo Non-Commercial License):**
   - Maintained in a completely separate repository (`fightcade-fbneo-fast`) strictly under the upstream FinalBurn Neo `src/license.txt` terms (non-commercial, zero donation links).
   - Never bundled inside the signed Fightcade Lab installer; offered only as an optional, consent-based SHA-256-verified external download from its separate release alongside verbatim `src/license.txt`.
4. **FFmpeg (`GPL-3.0`):**
   - Never bundled inside the Fightcade Lab installer; detected on `PATH` / local tools or fetched on first run with explicit user consent and SHA-256 verification.

---

## 2. Credits & Upstream Inspirations

- **Fightcade** (`fightcade.com`) & **fightcade-fbneo**: GGPO rollback netplay, public replay streaming, and FBNeo Lua bindings.
- **Grouflon (`3rd_training_lua`)**: Community reference for *Street Fighter III: 3rd Strike* memory layouts, hitbox presentation, and training mode design.
- **fbneo-training-mode**: Community reference for FBNeo RAM maps and Lua 5.1 training scripts.
- **Python Ecosystem:** `numpy` (BSD-3-Clause), `pandas` (BSD-3-Clause), `pyarrow` (Apache-2.0), `requests` (Apache-2.0), `pytest` (MIT).
