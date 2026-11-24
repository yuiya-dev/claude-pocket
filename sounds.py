"""The MapleStory sounds the page plays when a desk raises a lightbulb or a book.

They come from the community-run maplestory.io service, which serves the game's files as JSON.
Nothing of the game's ships with the office: each clip is fetched the first time it is played and
kept in the sounds/ folder next to the server (not in git). The game stores a clip as a short header
followed by an MP3; the header is cut off here so the browser can play what is left.
"""
import base64
import json
import os
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(ROOT, "sounds", "clips")
API = "https://maplestory.io/api/wz/"
# The only clips the server will fetch: the page asks for them by these ids
CLIPS = {"GMS/255/Sound/Game.img/QuestAlert", "GMS/255/Sound/UI.img/Invite"}


def clip(sid):
    """The MP3 bytes of one of CLIPS, fetched and cached on first use. Raises ValueError otherwise."""
    if sid not in CLIPS:
        raise ValueError("not one of the office's sounds")
    p = os.path.join(DIR, sid.replace("/", "~") + ".mp3")
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        pass
    req = urllib.request.Request(API + urllib.parse.quote(sid), headers={"User-Agent": "office-sounds/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = base64.b64decode(json.loads(r.read(8 * 1024 * 1024))["value"])
    start = next((i for i in range(40, min(len(raw) - 1, 400)) if raw[i] == 0xFF and raw[i + 1] & 0xE0 == 0xE0), None)
    if start is None:
        raise ValueError("this sound is not an MP3")
    os.makedirs(DIR, exist_ok=True)
    with open(p, "wb") as f:
        f.write(raw[start:])
    return raw[start:]
