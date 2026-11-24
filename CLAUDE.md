# Claude Pocket: notes for Claude and other AI assistants

## Installing it for a user

The same steps are in the README under "Instructions for Claude and other AI assistants".

A user has asked you to install Claude Pocket on their computer. It works only on Windows 10 or 11,
and only where you can run commands (Claude Code, or the Code tab in the Claude desktop app). In a
plain chat with no command access, give the user the one-command install from the README instead.

1. **Check the system.** Run `$env:OS` in PowerShell; it must be `Windows_NT`. On macOS or Linux,
   stop and tell the user Claude Pocket is Windows only.
2. **Check Python 3.** Run `py -3 --version`, then `python --version` if that fails. If neither
   prints `Python 3.x`, ask the user: *"Claude Pocket needs Python 3. May I install Python 3.12 with
   winget?"* Add `-Yes` below only if they agree. Without it, the installer stops when Python is
   missing rather than install it.
3. **Tell the user what will change, then install.** The installer copies one hook script into
   `%USERPROFILE%\.claude\hooks`, adds it to `%USERPROFILE%\.claude\settings.json` (keeping every
   other setting, with a backup in `settings.json.bak`), installs the app into
   `%LOCALAPPDATA%\Programs\Claude Pocket`, adds Start menu and desktop shortcuts, and opens the
   office. Do not edit `settings.json` yourself; the installer does it safely. Run, in PowerShell:

   ```powershell
   $env:CLAUDE_POCKET_ARGS = "-Yes"   # only if the user agreed to install Python; otherwise leave this line out
   irm https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.ps1 | iex
   ```

   From Git Bash, use single quotes so Bash leaves the `$` alone:

   ```bash
   powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 'irm https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.ps1 | iex'
   ```

   (Add `$env:CLAUDE_POCKET_ARGS="-Yes"; ` at the start of the quoted command if the user agreed.)
   `get.ps1` and `install.ps1` are short and safe to read first. If you would rather not pipe a
   download into PowerShell, clone the repository and run its installer instead:
   `git clone https://github.com/yuiya-dev/claude-pocket "$env:LOCALAPPDATA\Programs\Claude Pocket"`,
   then `powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Claude Pocket\install.ps1"`
   (with `-Yes` if agreed).
4. **Read the output.** Success prints green `Python:`, `Claude:`, `Hook:`, `Settings:` and
   `Shortcuts:` lines. A red message means it stopped; tell the user what it says.
5. **Check it is running.** Within about 10 seconds,
   `Invoke-RestMethod http://127.0.0.1:4545/config` answers with `"name": "Claude Pocket"`.
6. **Tell the user:** the office opened in its own window and is in the Start menu as
   **Claude Pocket**; their sessions appear at desks after their next message (if one doesn't,
   restart that session); and to remove it later, run `install.ps1 -Uninstall` from
   `%LOCALAPPDATA%\Programs\Claude Pocket`. Optional extras are the GitHub CLI (`gh auth login`) for
   pull requests on desks and the calendar button for Outlook/Teams meeting reminders.

## Working on the code

Read `NOTES.md` first: it explains how the server, the page, the hooks and the tray fit together,
and which parts rely on undocumented Claude app behaviour. There is no build step and no
dependency beyond the Python standard library. Keep personal data out of the repository; the files
the office writes while running are listed in `.gitignore`.
