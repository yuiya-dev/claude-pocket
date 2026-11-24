<#
  Sets up Claude Pocket for whoever runs it. It changes nothing outside your own user folder.

    powershell -ExecutionPolicy Bypass -File .\install.ps1              install and start the office
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Startup     also start it when you sign in to Windows
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall   take the hooks and the Startup shortcut out again

  What it does:
    1. Checks that Python 3 and Claude (the desktop app or Claude Code) are installed.
    2. Copies hooks\office_hook.py to %USERPROFILE%\.claude\hooks\.
    3. Adds that hook to %USERPROFILE%\.claude\settings.json for the seven events the office listens to,
       keeping every other setting and hook you already have. The old file is saved as settings.json.bak.
    4. Starts the office.
  Running it again is safe: it never adds the same hook twice.
#>
param([switch]$Startup, [switch]$Uninstall, [switch]$NoStart)
$ErrorActionPreference = "Stop"

$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$claude   = Join-Path $HOME ".claude"
$hookDir  = Join-Path $claude "hooks"
$hookFile = Join-Path $hookDir "office_hook.py"
$settings = Join-Path $claude "settings.json"
$launcher = Join-Path $here "Start Claude Pocket.vbs"
$startDir = [Environment]::GetFolderPath("Startup")
if (-not $startDir) { $startDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup" }
$shortcut = Join-Path $startDir "Claude Pocket.lnk"
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
  if (Test-Path $shortcut) { Remove-Item $shortcut }
  Say "Claude Pocket's hooks are removed. Quit the office from its tray icon, then delete this folder if you like." "Green"
  exit 0
}

# ---------- 1. Python ----------
$python = $null
foreach ($try in "py -3", "python", "python3") {
  $cmd, $extra = $try -split " "
  try {
    $exe = & $cmd @($extra | Where-Object { $_ }) -c "import sys; print(sys.executable if sys.version_info[0] == 3 else '')" 2>$null
    if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { $python = $exe.Trim(); break }
  } catch { }
}
if (-not $python) {
  Stop-With ("Python 3 is not installed.`n" +
    "Install it with:   winget install Python.Python.3.12`n" +
    "or from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),`n" +
    "then open a new PowerShell window and run this again.")
}
Say "Python:  $python" "Green"

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
if ($Startup) {
  $sh = New-Object -ComObject WScript.Shell
  $lnk = $sh.CreateShortcut($shortcut)
  $lnk.TargetPath = "wscript.exe"
  $lnk.Arguments = "`"$launcher`" quiet"
  $lnk.WorkingDirectory = $here
  $lnk.Save()
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
