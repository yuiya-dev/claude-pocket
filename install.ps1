<#
  Sets up Claude Pocket for whoever runs it. It changes nothing outside your own user folder.

    powershell -ExecutionPolicy Bypass -File .\install.ps1              install and start the office
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Startup     also start it when you sign in to Windows
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -NoDesktop   no shortcut on the desktop (Start menu only)
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Yes         install Python without asking, if it is missing
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall   take the hooks and the shortcuts out again
  (Install.cmd runs the first one when you double-click it.)

  What it does:
    1. Checks that Python 3 and Claude (the desktop app or Claude Code) are installed, and offers to
       install Python with winget when it is missing.
    2. Copies hooks\office_hook.py to %USERPROFILE%\.claude\hooks\.
    3. Adds that hook to %USERPROFILE%\.claude\settings.json for the seven events the office listens to,
       keeping every other setting and hook you already have. The old file is saved as settings.json.bak.
    4. Adds Claude Pocket to the Start menu and the desktop, and starts the office.
  Running it again is safe: it never adds the same hook twice.
#>
param([switch]$Startup, [switch]$NoDesktop, [switch]$Yes, [switch]$Uninstall, [switch]$NoStart)
$ErrorActionPreference = "Stop"

$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$claude   = Join-Path $HOME ".claude"
$hookDir  = Join-Path $claude "hooks"
$hookFile = Join-Path $hookDir "office_hook.py"
$settings = Join-Path $claude "settings.json"
$launcher = Join-Path $here "Start Claude Pocket.vbs"
function Get-Folder($kind, $fallback) {
  $f = [Environment]::GetFolderPath($kind)
  if ($f) { return $f } else { return $fallback }
}
$programs = Get-Folder "Programs" (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs")
$shortcut = Join-Path (Get-Folder "Startup" (Join-Path $programs "Startup")) "Claude Pocket.lnk"
$menuLink = Join-Path $programs "Claude Pocket.lnk"
$deskLink = Join-Path (Get-Folder "Desktop" (Join-Path $HOME "Desktop")) "Claude Pocket.lnk"
$pyFile   = Join-Path $here "python-path.txt"  # the launcher reads it, so Python need not be on PATH
$events   = "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Stop-With($text) { Say ""; Say $text "Red"; Say ""; exit 1 }

function Write-Settings($obj) {
  if (Test-Path $settings) { Copy-Item $settings "$settings.bak" -Force }
  $json = $obj | ConvertTo-Json -Depth 32
  [IO.File]::WriteAllText($settings, $json, (New-Object Text.UTF8Encoding $false))  # no BOM
}

function Read-Settings {
  if (-not (Test-Path $settings)) { return [pscustomobject]@{} }
  $text = [IO.File]::ReadAllText($settings)
  if (-not $text.Trim()) { return [pscustomobject]@{} }
  try { return $text | ConvertFrom-Json }
  catch { Stop-With "Your $settings is not valid JSON, so it was left alone. Fix it (or move it away) and run this again." }
}

function New-Link($path) {
  if (-not (Split-Path $path)) { return }
  New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
  $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
  $lnk.TargetPath = "wscript.exe"
  $lnk.Arguments = "`"$launcher`"" + $(if ($path -eq $shortcut) { " quiet" } else { "" })
  $lnk.WorkingDirectory = $here
  $lnk.IconLocation = (Join-Path $here "icon.ico") + ",0"
  $lnk.Description = "Your Claude Code sessions as characters at desks"
  $lnk.Save()
}

function Find-Python {
  foreach ($try in "py -3", "python", "python3") {
    $cmd, $extra = $try -split " "
    try {
      $exe = & $cmd @($extra | Where-Object { $_ }) -c "import sys; print(sys.executable if sys.version_info[0] == 3 else '')" 2>$null
      if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    } catch { }
  }
  return $null
}

# Files downloaded in a ZIP are marked as coming from the internet, which makes Windows warn every
# time the launcher runs. These are the files you chose to install, so take the mark off.
Get-ChildItem $here -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

function Is-OfficeHook($group) {
  foreach ($h in @($group.hooks)) {
    if ("$($h.command) $($h.args -join ' ')" -like "*office_hook.py*") { return $true }
  }
  return $false
}

# ---------- uninstall ----------
if ($Uninstall) {
  $s = Read-Settings
  if ($s.PSObject.Properties["hooks"]) {
    foreach ($e in $events) {
      if ($s.hooks.PSObject.Properties[$e]) {
        $keep = @(@($s.hooks.$e) | Where-Object { -not (Is-OfficeHook $_) })
        if ($keep.Count) { $s.hooks.$e = $keep } else { $s.hooks.PSObject.Properties.Remove($e) }
      }
    }
    if (-not @($s.hooks.PSObject.Properties).Count) { $s.PSObject.Properties.Remove("hooks") }
    Write-Settings $s
  }
  if (Test-Path $hookFile) { Remove-Item $hookFile }
  foreach ($f in $shortcut, $menuLink, $deskLink, $pyFile) { if ($f -and (Test-Path $f)) { Remove-Item $f } }
  Say "Claude Pocket's hooks and shortcuts are removed. Quit the office from its tray icon, then delete this folder if you like." "Green"
  exit 0
}

# ---------- 1. Python ----------
$python = Find-Python
if (-not $python -and (Get-Command winget -ErrorAction SilentlyContinue)) {
  Say "Python 3 is not installed. Claude Pocket needs it (nothing else)." "Yellow"
  # Someone at the keyboard is asked. When no one can answer (an AI assistant ran this, or the
  # input is not a console), it stops instead, so the person can be asked and run it again with -Yes.
  if ($Yes) { $answer = "y" }
  elseif ([Console]::IsInputRedirected -or -not [Environment]::UserInteractive) {
    Stop-With ("Python 3 is not installed, and no one is here to answer whether to install it.`n" +
      "Ask the user, and if they agree, run this again with -Yes to install Python 3.12 with winget.")
  }
  else { $answer = Read-Host "Install Python 3.12 now with winget? [Y/n]" }
  if ($answer -notmatch '^(n|no)$') {
    winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $python = Find-Python
    if (-not $python) {  # winget's own place for a per-user install, in case PATH has not caught up
      $python = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\Python") -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    }
  }
}
if (-not $python) {
  Stop-With ("Python 3 is not installed.`n" +
    "Install it with:   winget install Python.Python.3.12`n" +
    "or from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),`n" +
    "then run this again.")
}
Say "Python:  $python" "Green"
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
[IO.File]::WriteAllText($pyFile, $pythonw, [Text.Encoding]::Default)  # the launcher (VBScript) reads the ANSI code page

# ---------- 2. Claude ----------
if (-not (Test-Path $claude)) {
  Stop-With ("Claude has not been set up on this PC yet ($claude does not exist).`n" +
    "1. Install the Claude desktop app from https://claude.ai/download (or Claude Code).`n" +
    "2. Open it and sign in to your own Claude account.`n" +
    "3. Open the Code tab and start one session, so Claude creates its folder.`n" +
    "Then run this again.")
}
Say "Claude:  $claude" "Green"

# ---------- 3. the hook ----------
New-Item -ItemType Directory -Force $hookDir | Out-Null
Copy-Item (Join-Path $here "hooks\office_hook.py") $hookFile -Force
Say "Hook:    $hookFile" "Green"

$s = Read-Settings
if (-not $s.PSObject.Properties["hooks"]) { $s | Add-Member hooks ([pscustomobject]@{}) }
$entry = [pscustomobject]@{ hooks = @([pscustomobject]@{
  type = "command"; command = $python.Replace("\", "/"); args = @($hookFile.Replace("\", "/")); timeout = 5 }) }
$added = 0
foreach ($e in $events) {
  $groups = @()
  if ($s.hooks.PSObject.Properties[$e]) { $groups = @($s.hooks.$e) }
  if ($groups | Where-Object { Is-OfficeHook $_ }) { continue }
  $groups += $entry
  $s.hooks | Add-Member -Force $e $groups
  $added++
}
if ($added) { Write-Settings $s; Say "Settings: added the hook to $added event(s) in $settings" "Green" }
else { Say "Settings: the hook was already there" "Green" }

# ---------- 4. optional extras ----------
New-Link $menuLink
if ($NoDesktop) { Say "Shortcuts: Claude Pocket is in the Start menu" "Green" }
else { New-Link $deskLink; Say "Shortcuts: Claude Pocket is in the Start menu and on your desktop" "Green" }
if ($Startup) {
  New-Link $shortcut
  Say "Startup: Claude Pocket starts (tray icon only) when you sign in" "Green"
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  Say "Optional: install the GitHub CLI (winget install GitHub.cli), then run 'gh auth login', to see pull requests on desks." "Yellow"
}

# ---------- 5. start ----------
if ($NoStart) { exit 0 }
Start-Process wscript.exe -ArgumentList "`"$launcher`""
Say ""
Say "Claude Pocket is starting. Send a message in any Claude Code session and its character walks in." "Cyan"
Say "Sessions that were already open show up after their next message. Restarting Claude is not needed." "Cyan"
