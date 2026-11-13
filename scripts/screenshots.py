"""Rebuild the showcase screenshots in docs/img/ (README, website, presentations).

Runs the demo showcase into a temporary folder (synthetic data only) and photographs it
with a locally installed Edge or Chrome in headless mode - no Python dependency.

    uv run python scripts/screenshots.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mailassist import showcase
from mailassist.config import Settings

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"
# (file name, page anchor, width, height, dark mode)
SHOTS = [
    ("schaufenster-anfrage.png", "anfrage", 1280, 1160, False),
    ("schaufenster-manipulation.png", "manipulation-naiv", 1280, 1240, False),
    ("schaufenster-versteckt.png", "versteckt", 1280, 1280, False),
    ("schaufenster-manipulation-dunkel.png", "manipulation-naiv", 1280, 1240, True),
    ("schaufenster-handy.png", "manipulation-naiv", 500, 2260, False),
]
CANDIDATES = [
    "msedge", "chrome", "chromium", "google-chrome",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def browser() -> str:
    for name in CANDIDATES:
        found = shutil.which(name) or (name if os.path.isfile(name) else None)
        if found:
            return found
    sys.exit("Kein Edge/Chrome gefunden – für die Screenshots wird ein Chromium-Browser benötigt.")


def main() -> None:
    exe = browser()
    OUT.mkdir(parents=True, exist_ok=True)
    # Windows keeps the demo's SQLite ledger open until the process ends.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = Settings.for_demo({"MAILASSIST_DEMO_DATA_DIR": tmp})
        page = asyncio.run(showcase.write(settings, Path(tmp) / "schaufenster.html"))
        for name, anchor, width, height, dark in SHOTS:
            target = OUT / name
            args = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--window-size={width},{height}", f"--screenshot={target}", f"--user-data-dir={Path(tmp) / 'profil'}"]
            if dark:
                args.append("--force-dark-mode")
            subprocess.run([*args, f"{page.resolve().as_uri()}#{anchor}"], check=True, capture_output=True, timeout=60)
            print(f"{target.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    main()
