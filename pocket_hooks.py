"""Adds Claude Pocket's hook to Claude's settings.json, or takes it out again. Used by install.sh on
macOS (install.ps1 does the same on Windows).

    python3 pocket_hooks.py install   SETTINGS_JSON PYTHON HOOK_FILE
    python3 pocket_hooks.py uninstall SETTINGS_JSON

Every other setting and hook is kept. Before the first change, a copy of the file is saved as
settings.json.claude-pocket.bak, and later runs never overwrite it. Installing twice adds nothing.
"""
import json
import os
import shutil
import sys

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd")
MARK = "claude_pocket_hook.py"


def ours(group):
    return any(MARK in "%s %s" % (h.get("command", ""), " ".join(map(str, h.get("args") or [])))
               for h in (group.get("hooks") or []) if isinstance(h, dict))


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        sys.exit("Your %s is not valid JSON, so it was left alone. Fix it (or move it away) and run this again." % path)
    if not isinstance(data, dict):
        sys.exit("Your %s is not a JSON object, so it was left alone." % path)
    return data


def save(path, data):
    backup = path + ".claude-pocket.bak"
    if os.path.exists(path) and not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def install(path, python, hook):
    data = load(path)
    hooks = data.setdefault("hooks", {})
    entry = {"hooks": [{"type": "command", "command": python, "args": [hook], "timeout": 5}]}
    added = 0
    for e in EVENTS:
        groups = hooks.get(e) or []
        if any(ours(g) for g in groups if isinstance(g, dict)):
            continue
        hooks[e] = groups + [entry]
        added += 1
    if added:
        save(path, data)
        print("Settings: added the hook to %d event(s) in %s" % (added, path))
    else:
        print("Settings: the hook was already there")


def uninstall(path):
    data = load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for e in EVENTS:
        if e in hooks:
            keep = [g for g in hooks[e] if not (isinstance(g, dict) and ours(g))]
            if keep:
                hooks[e] = keep
            else:
                del hooks[e]
    if not hooks:
        del data["hooks"]
    save(path, data)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "install":
        install(*sys.argv[2:])
    elif len(sys.argv) == 3 and sys.argv[1] == "uninstall":
        uninstall(sys.argv[2])
    else:
        sys.exit(__doc__)
