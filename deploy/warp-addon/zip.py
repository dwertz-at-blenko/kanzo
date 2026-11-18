# Version: v1 · created 2026-11-15 05:45 EST · updated 2026-11-15 05:45 EST
# Changes: v1 zips the add-on for the board to serve.
# This file: /home/claude/dev/kanzo/.claude/worktrees/warp-splash/deploy/warp-addon/zip.py
# Session: 1434c01e-24f9-406f-856d-7015b5dd7b69 · vps as claude · cwd /home/claude/dev · slug unified-imagining-globe
# Resume: ssh -t blenko-claude 'cd /home/claude/dev && claude --resume 1434c01e-24f9-406f-856d-7015b5dd7b69'   (from the Mac)
"""Zip the sign-in stars add-on into src/warp-addon.zip, the file the board serves at
/warp-addon.zip (Settings > Board > Warp stars > "Stars at sign-in too").

Run it after any change to manifest.json, stars.css or stars.js:

    python3 deploy/warp-addon/zip.py

Stored, not compressed, with fixed dates, so the same inputs give the same bytes and a test
in src/web.rs can check the zip still holds the three files as they are in this folder.
"""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "src", "warp-addon.zip")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_STORED) as z:
    for name in ("manifest.json", "stars.css", "stars.js"):
        info = zipfile.ZipInfo("kanzo-warp-addon/" + name, date_time=(2026, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        with open(os.path.join(HERE, name), "rb") as f:
            z.writestr(info, f.read())
print("wrote", os.path.normpath(OUT))
