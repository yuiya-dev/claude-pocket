"""Forwards a Claude Code hook event to the Claude Pocket server.

Always exits 0 and stays silent, so sessions are unaffected when the server is not running.
"""
import os
import sys
import urllib.request

try:
    data = sys.stdin.buffer.read()
    port = os.environ.get("OFFICE_PORT", "4545")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/event", data=data,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.open(req, timeout=0.3).read()
except Exception:
    pass
sys.exit(0)
