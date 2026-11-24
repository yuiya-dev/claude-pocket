# Claude Pocket — notes for whoever works on this next

A local dashboard at http://localhost:4545 that shows every Claude Code session on this PC as a
Pokémon at a desk. Pure Python standard library, one page, no build step.

## Settings (`office.json`)

| Key | Default | What it does |
|---|---|---|
| `name` | Claude Pocket | The name in the header, the window title and the tray |
| `themes` | pokemon, maple | Which casts the theme button offers; with one, the button is hidden |
| `stocks` | off | The header stock widget |
| `lan` | off | Lets other devices on your network (a phone) open the office |

`office.json` is read once at start-up and served at `/config`. With stocks off the widget is
hidden, the stock loop never starts and the stock endpoints answer empty.

## How it works

- **Hooks → server.** `~/.claude/settings.json` has a `hooks` section that runs
  `~/.claude/hooks/office_hook.py` on SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
  Notification, Stop and SessionEnd. The hook POSTs the event JSON to `http://127.0.0.1:4545/event`
  and gives up silently after 0.3 s if the server is down. A copy of the script is kept in
  `hooks/office_hook.py`; the one Claude actually runs is the copy under `~/.claude/hooks`.
- **`server.py`** keeps one record per session (`state.json`) and serves `index.html`. Background
  loops:
  - `resolve_loop`: maps hook session ids to the desktop app's ids and titles by reading the app's
    session records under `%LOCALAPPDATA%\Claude-Data\*\claude-code-sessions`, checks PR status and
    review decision with `gh`, applies the review hold, and `reconcile()` catches turn endings whose
    hook event was missed.
  - `stock_loop`: TWSE live feed for Taiwan codes, Yahoo Finance otherwise.
  - `credit_loop`: reads every transcript under `~/.claude/projects` once, then only the ones that
    changed, to learn which session worked on which pull request (`credits.json`).
  - `merged_loop`: keeps this week's merged pull requests fetched (`merged.json`).
  - `meetings.calendar_loop` and `meetings.remind_loop`: meeting reminders, see below.
- **`index.html`** polls `/state` every second and draws the room. All styling and script are inline.

## Meeting reminders (`meetings.py`)

- **Source.** The ICS link of a calendar published from Outlook on the web, kept in
  `calendar-link.txt` (not in git; anyone with the link can read the calendar). `POST
  /meetings/link` (`url`) sets it, from this computer only. `webcal://` is turned into `https://`.
- **Parsing.** Standard library only. Python on Windows has no time zone database, so zones come from
  the file's own VTIMEZONE blocks (yearly `BYMONTH` + `BYDAY` rules, as Outlook writes them). Repeating
  meetings are expanded day by day for the next 7 days (`AHEAD`), which is also what the page lists: DAILY, WEEKLY, MONTHLY and YEARLY with
  INTERVAL, COUNT, UNTIL, BYDAY (including `2TU` / `-1FR` and BYSETPOS), BYMONTHDAY and BYMONTH;
  EXDATE skips one, and a VEVENT with RECURRENCE-ID replaces one. All-day events, `STATUS:CANCELLED`
  and titles starting "Canceled:" are skipped. The Teams join link is taken from
  `X-MICROSOFT-SKYPETEAMSMEETINGURL` or the first `teams.microsoft.com/l/meetup-join` link in the
  description.
- **Reminders.** Each meeting has a reminder for each of `LEADS` (10, 5 and 1 minutes), keyed
  `<uid>@<start>|<lead>`. Each is due until the next one takes over; the 1-minute one is due until
  5 minutes after the start. `GET /meetings` returns the meetings and the due reminders (`alerts`); `POST /meetings/ack`
  (`key`) acknowledges one. Acknowledged keys are kept for 3 days in `meeting-acks.json`, so a restart
  does not show them again.
- **Pop-up.** For each due reminder `remind_loop` starts a hidden PowerShell that shows a topmost
  WinForms window with Got it (and Join in Teams when there is a link). It can't be closed any other
  way, and no control takes keyboard focus, so only a click closes it (Enter typed into another window
  as it opens must not dismiss it). It exits with code 7 only when one of its buttons was pressed,
  which acknowledges the reminder; any other exit leaves it due, and it is opened again 30 s later. The
  meeting title reaches it through environment variables, never through the script text. When a
  reminder is acknowledged from the page, or stops being due, the server kills its window. The
  window loops `%SystemRoot%\Media\Alarm01.wav` (`SoundPlayer.PlayLooping`, which ignores the Windows
  sound scheme) until it closes; killing it stops the sound too.
- **Page.** Polls `/meetings` every 3 seconds. A due reminder shows as a full-screen banner above
  everything, with a flashing tab title. On another device the page also loops the same alarm, served
  at `/alarm`, until Got it; on this computer it stays quiet so the alarm doesn't ring twice.

## MapleStory sounds (`sounds.py`)

maplestory.io serves the game's files as JSON under `https://maplestory.io/api/wz/<region>/<version>/`:
a folder is `{"children": [...]}`, a sound is `{"type": 11, "value": "<base64>"}`. The value is an
82-byte header (the game's sound descriptor) followed by an MP3; `clip()` cuts at the first MPEG frame
sync. `/sound?id=` serves one of `CLIPS` (only those, so the server never fetches an arbitrary path),
cached in `sounds/clips/` after the first play. Browse other sounds with e.g.
`https://maplestory.io/api/wz/GMS/255/Sound/UI.img`.

## Tray icon and window (`tray.py`)

- **Icon.** Once the port is bound, the server starts a hidden PowerShell that shows a WinForms
  `NotifyIcon` drawn from `icon.png` (without the file it is Windows' plain program icon). Left click posts `/show`; the menu has Open (`/show`) and Quit
  (`/quit`). A timer in it checks every 2 s that the server's process still exists and exits when it
  does not, so a killed server leaves no icon behind.
- **`/show` and `/quit`.** Accepted only from this computer and only without an `Origin` header,
  which a browser always sends with a POST: the tray icon and the launcher can ask, a web page open
  in a browser here cannot. `/quit` ends the server process at once.
- **Window.** `show()` looks for the office's window: a visible top-level window of `chrome.exe` or
  `msedge.exe` whose title ends in the office's name (an app window's title is the page's title
  alone; a tab's has the browser's name after it), or is the title the page flashes during a meeting
  reminder (`ALARM_TITLE`, keep it the same as in `index.html`). Found, it is brought forward (a
  tap of Alt first, as for Claude's window). Otherwise Chrome, else Edge, is started with
  `--app=http://localhost:<port>/`, and the new window is brought forward once it exists, because a
  window opened by a background program can come up behind the others. With neither browser the
  page opens as an ordinary tab.
- **Profile.** The window has its own browser profile in `%LOCALAPPDATA%\<office name>\window`,
  not next to the server, since that folder may be synced. Its own profile makes it a separate
  program from the user's browser, which is what lets `--autoplay-policy=no-user-gesture-required`
  apply (a browser that is already running ignores flags), so desk sounds play without a click.
  The cost is that permissions start over: the page asks once for location. Deleting the folder is
  safe. `--start-maximized` is passed only when the folder does not exist yet: a new window is
  under 1000 px wide, where the page drops to its compact layout (no board, half-size markers);
  later opens come back where the window was left.
- **Launcher.** `Start Claude Pocket.vbs` starts the server only when `/config` does not answer, then
  posts `/show` unless it was given an argument. It no longer stops a running copy: to load new
  server code, Quit from the tray and start again.

## Desk states

| Shown as | Look | When |
|---|---|---|
| `working` | typing screen | prompt submitted or tool running |
| `waiting` | awake and still, "Waiting · background task" | turn ended only to wait on its own background job |
| `needs_input` | lightbulb marker | turn ended on a question / go-ahead request, or a permission or choice prompt |
| `done` | book marker | turn ended with nothing asked; cleared by clicking the desk or opening the session in Claude |
| `review` | awake, purple, "In review · #N" | turn ended and a pull request the session created is open and not yet approved |
| `resting` | grey, "z"s | after `done` is cleared |

`waiting` is not a stored state either: the server stores `working` with the tool name
`background task`, and the page draws that pair as `waiting`.

`review` is not a stored state: the server stores `resting` plus a `waiting_pr` list, and the page
draws that pair as `review` (`shown()` in `index.html`). The rule is `review_hold()` in `server.py`:
the book is held back while the pull request waits for review, and appears once it is approved or
merged. Closed unmerged, or turned back into a draft, releases the hold with no book. Drafts never
hold the book.

The question / waiting detection is text-based (`ASKING`, `WAITING` regexes in `server.py`) plus the
desktop app's end-of-turn `wrap_up` tool call (`ask` present → needs input).

**Marker sounds.** `markSounds()` in the page runs before each render and compares every desk's
`shown()` state with the last poll's: a desk newly showing `needs_input` plays MapleStory's
`Game.img/QuestAlert`, newly showing `done` plays `UI.img/Invite` (both GMS 255, served through
`/sound`, see MapleStory sounds). Because it goes by `shown()`, asleep desks and the review hold stay
quiet. The first poll after the page loads only records states, and several desks changing in one
poll sound once (a lightbulb first, a book 1.2 s later if both). Each device with the page open plays
its own; a browser blocks the sound until the page has been clicked since it loaded.

## Pull requests on a desk

A desk shows only pull requests its own session created: `gh pr create` whose output has the new
link on a line of its own (`created_prs()`). Live hook events and the transcript index both feed
`pr_made`; the newest three by number are shown. Links a session merely mentions never appear.

## Themes

The **Theme** button in the header switches the cast between Pokémon and MapleStory monsters
(`/theme`, saved in `settings.json`). Every desk keeps one of each: `mon` for its Pokémon and
`maple` for its monster, so switching never loses a pick. A desk with no `maple` yet gets one
chosen from its session id by the page (`mapleOf()`), from the ordinary monsters only, never a boss.

The monsters come from `maple.json` (served at `/maple`): about 460 entries of
`[id, name, level, boss]`. They are the world monsters of GMS v92, the last version before Big
Bang, plus the main bosses added since (Magnus, Lotus, Damien, Black Mage and so on). Each one was
checked to have a standing sprite when the list was built. A saved pick that is no longer in the
list is ignored and the desk gets an automatic monster.
Sprites are drawn straight from maplestory.io's animated images. Not every monster has a standing
sprite and the list is not checked against that, so the page copes instead: the picker removes a
cell whose picture fails, and a desk steps on to the next monster. On desks every sprite is drawn
at the same whole-number magnification (`fitMaple()`), so pixels match and sizes stay in proportion.

Pokémon on desks are sized by real height (`POKE_H`, `pokeEm()`), on a compressed scale.

## Background music

In MapleStory mode the header shows a small player when the `music` folder next to the server has
audio files in it (mp3, ogg, m4a, wav, flac). `/music` lists them and `/music/<name>` serves one.
The folder is ignored by git and nothing ships in it: the soundtrack is not ours to distribute. The
player shuffles and loops, stops outside MapleStory mode, and remembers volume and on/off in the
browser. Browsers refuse sound until the page has been clicked, so after a refresh it resumes on
the first click.

## Sleep

The rename dialog has a "Put this Pokémon to sleep" checkbox (`/sleep`, the `asleep` flag). It is a
one-time mute: the page hides that desk's lightbulb, book and in-review look, and the flag clears
itself the next time the session does any work.

## To-do list

Header button right of the office name, with the number of open items on it. The list lives in
`todos.json` on the server, so the computer and a phone share it. `GET /todos` returns it;
`POST /todos/add` (`text`), `/todos/done` (`id`, `done`) and `/todos/remove` (`id`) change it and
answer with the whole list, which the page redraws from. The page shows open items first, each
group oldest first.

## Merged PRs panel

Header button, left of New session. `/merged?week=N` returns the signed-in GitHub user's pull
requests merged in the N-th week back, through `gh api search/issues`. It asks a week at a time
because GitHub's search cannot sort by merge date, which also avoids its 1,000-result limit. Answers
come from `merged.json` at once and are refreshed in the background when stale.

Each day heading carries the number merged that day, counted in the page from the rows loaded so
far. The oldest day shown can undercount until "Load older" brings in the week before it.

Each row credits one Pokémon: the session that created the pull request, else the one that worked on
it most (`credits_of()`). Scores come from `scan_transcript()`: 1000 for creating it, 3 for a
`gh pr` command on it, 1 each time the user or Claude wrote its link. Command output is ignored.
Raise `CREDIT_RULE` when that scoring changes, so the saved index is rebuilt. Sessions whose
transcripts are not on this PC (cloud sessions, other machines) get no credit.

## Stock chart

Clicking a stock in the header opens a modal with a price graph drawn by the page itself (inline
SVG, no library). `/chart?symbol=&range=` proxies Yahoo Finance's chart feed for symbols on the
watchlist only. 1D falls back to the last day that traded when today has no trades. Colours follow
the header tiles: red is up, green is down.

## Opening a session from a desk

The page POSTs `/open` and the server launches `claude://claude.ai/epitaxy/<app id>`, which both
switches the session and raises Claude's window. Measured over covered-window trials: about 1.0 s
from click to the session being displayed.

About one switch in eight, Claude comes forward and keeps showing the old session until its window
is moved. So after the link the server watches the window: it compares small blurred pictures of
the middle of it (in memory only), and if nothing has changed 1.6 s after the click it restores and
re-maximizes the window (or shifts a non-maximized one a few pixels and back), again at 2.9 s and
4.2 s if needed. That freed every stuck switch in testing; one-pixel nudges and redraw requests
never did. Clicking the desk of the session opened last skips the poke, since the picture is right
to stay as it is. Each open is timed into `clicks.log` (`changed_ms`, `pokes`).

The older order (raise the window with `claude://code/office-bring-to-front`, send the session
link once it is in front) took about 1.7 s; `/open` still accepts `"trial": {"direct": false}` to
measure it again.

## Things that are not documented app behaviour and may break on an app update

- The session-record JSON format (`cliSessionId`, `sessionId`, `title`, `lastFocusedAt`).
- The `claude://` link formats.
- `stop_hook_summary` entries in transcripts, used by `reconcile()`.
- The transcript line format read by `scan_transcript()` (`tool_use` / `tool_result` blocks).
- Yahoo Finance's unofficial chart feed.

## Other files

- `markers/bulb.png`, `markers/book.png`: animated quest markers (drop in replacements to change them).
- `stocks.json`: header stock list, written when you add a stock (not kept in git).
- `logo-maple.png` (optional, not included): a logo to show in MapleStory mode; without it the
  page keeps its drawn one.
- `Start Claude Pocket.vbs`: launcher; a shortcut in the Windows Startup folder runs it at login
  with the argument `quiet`, which leaves the window closed.
- Written while running and not kept in git: `state.json` (desks), `hidden.json` (sessions removed
  from the office), `credits.json`, `merged.json`, `clicks.log` (timing notes for desk clicks).
  All are safe to delete; the office rebuilds what it needs.

## Known limits

- Cloud sessions never appear (hooks only run on this PC).
- Pokémon icons, fonts and weather need internet.
- Rename is office-only; it does not rename the session in the Claude app.
- A pull request opened some other way than `gh pr create` does not show on a desk.

Now and then the link does not bring Claude's window forward at all (seen for a minute on
2026-11-15, cause not found). If Claude is not in front one second after the link, the server
brings its window forward itself (`raise_claude`: a tap of Alt, then `SetForegroundWindow`) and
notes it in `clicks.log` as `raised`.

## Opening the office on a phone

Off unless `office.json` has `"lan": true`. With it on, the server listens on the whole network
instead of only this computer, and writes the phone address to `phone-link.txt` at startup. Any
device other than this computer must show the private key from `lan-key.txt`: once as `?key=` in
the address, which is swapped for a cookie, and by that cookie afterwards. Both files are
git-ignored. Windows Firewall asks once to allow Python on private networks.

`/config` tells the page whether it is open on another device (`remote`). There a desk is a real
link to `https://claude.ai/code/<Remote Control id>`, which opens that session in the Claude mobile
app; the id is the last entry of `bridgeSessionIds` in the desktop app's session record (`rc_id` in
`/state`). `/open` is ignored from other devices, so a phone never moves Claude's window on the
computer. A session that has never been connected to Remote Control has no id, and the desk says
so. The desk buttons stay visible on touch screens and one of them changes the character, since
there is no hover or right-click. Music is served in byte ranges because phones will not play it
otherwise. Dragging desks into a new order is not set up for touch.

Away from home: with Tailscale installed and signed in on this computer and on the phone, the
server also writes this computer's Tailscale address (`tailscale ip -4`, a `100.x` address that
does not change) to `phone-link.txt`. That link works on any network while Tailscale is switched
on in the phone. Nothing is opened to the public internet, and the key is still required.

If the phone cannot reach it over Tailscale, look at Windows Firewall. Tailscale normally adds
its own inbound rule ("Tailscale-In"), which is enough. Without it, the "allow Python" prompt
does not help, because it only covers the profile the Wi-Fi is on and the Tailscale adapter is a
different one; a rule can be added from an admin terminal:
`New-NetFirewallRule -DisplayName "Office over Tailscale" -Direction Inbound -Action Allow
-Protocol TCP -LocalPort 4545 -InterfaceAlias "Tailscale"`. Testing the Tailscale address from
this computer itself does not go through the firewall, so only a phone on mobile data proves it.

## Starting a session on the computer from the phone

The Claude phone app starts cloud sessions unless the computer offers itself. The Claude desktop
app does that: Settings > Code > Remote Control, switched on, with the folders to offer listed
there ("Add folder..."). Each listed folder then shows under "Remote Control" in the phone app's
environment list, with a "New session" button. A session started that way runs in the desktop
app on the computer, so it appears in the app's sidebar and gets a desk like any other. The
phone's "+ New session" box in the office only explains those taps; nothing here starts sessions.

Such a session's app record holds its Remote Control id as `cse_...` rather than `session_...`
(`bridgeSessionIds`, and `remoteControlSpawn.ccrSessionId`). It is the same id with another
prefix; `rc_id` turns it into the `session_...` form the page address uses.

Command-line sessions with Remote Control (`claude --remote-control`, or ones started under a
`claude remote-control` server) have no app record. Their id comes from the command-line tool's
own files, `~/.claude/sessions/<pid>.json` (`bridgeSessionId`), so their desks get a phone link
too; on the computer a click opens their Remote Control page in the browser, since the desktop
app cannot show them while they run. The same files say when such a session went idle, which
`reconcile` uses when no turn-ended signal arrived.

What was tried and dropped, so nobody tries again: the office running `claude remote-control`
servers itself (works, but those sessions never appear in the desktop app's sidebar, the app
refuses to serve a folder a server already holds, and a killed server lingers in the phone's
list); a "launcher" session that starts others (the app refuses to start a session from a turn
that arrives over Remote Control); the app's `claude://code/new?folder=&q=` link (only fills in
the message box); adopting a command-line session with `claude://resume?session=` (not connected
to Remote Control).
