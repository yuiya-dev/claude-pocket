# Downloads (or updates) Claude Pocket and runs its installer. Paste this into PowerShell:
#
#   irm https://raw.githubusercontent.com/yuiya-dev/claude-pocket/main/get.ps1 | iex
#
# It puts the app in %LOCALAPPDATA%\Programs\Claude Pocket. Running it again updates the app and keeps
# your office.json and everything the office has saved (desks, to-dos, calendar link).
# To pass the installer options, set them first, for example:
#   $env:CLAUDE_POCKET_ARGS = "-Startup -NoDesktop"
& {
  $ErrorActionPreference = "Stop"
  $zipUrl = "https://github.com/yuiya-dev/claude-pocket/archive/refs/heads/main.zip"
  $dest   = if ($env:CLAUDE_POCKET_DIR) { $env:CLAUDE_POCKET_DIR } else { Join-Path $env:LOCALAPPDATA "Programs\Claude Pocket" }
  $tmp    = Join-Path ([IO.Path]::GetTempPath()) ("claude-pocket-" + [guid]::NewGuid().ToString("N"))

  try {
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $zip = Join-Path $tmp "claude-pocket.zip"
    if ($env:CLAUDE_POCKET_ZIP) {
      Copy-Item $env:CLAUDE_POCKET_ZIP $zip
    } else {
      Write-Host "Downloading Claude Pocket..." -ForegroundColor Cyan
      [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
      $ProgressPreference = "SilentlyContinue"  # the progress bar makes downloads very slow in Windows PowerShell
      Invoke-WebRequest $zipUrl -OutFile $zip -UseBasicParsing
    }
    Expand-Archive $zip (Join-Path $tmp "x") -Force
    $src = Get-ChildItem (Join-Path $tmp "x") -Directory | Select-Object -First 1
    if (-not $src -or -not (Test-Path (Join-Path $src.FullName "install.ps1"))) { throw "The download did not contain Claude Pocket." }

    # A running office is stopped first, so the new files are the ones that start.
    $opts = @("$env:CLAUDE_POCKET_ARGS" -split "\s+" | Where-Object { $_ })
    if ($opts -notcontains "-NoStart") { try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:4545/quit" -Body "{}" -ContentType "application/json" -TimeoutSec 2 | Out-Null; Start-Sleep -Seconds 1 } catch { } }

    $update = Test-Path (Join-Path $dest "install.ps1")
    New-Item -ItemType Directory -Force $dest | Out-Null
    foreach ($item in Get-ChildItem $src.FullName -Force) {
      if ($item.Name -eq "office.json" -and (Test-Path (Join-Path $dest "office.json"))) { continue }  # keep your settings
      Copy-Item $item.FullName $dest -Recurse -Force
    }
    Write-Host ($(if ($update) { "Updated" } else { "Installed" }) + " in $dest") -ForegroundColor Green

    $installArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $dest "install.ps1"))
    $installArgs += $opts
    & powershell.exe @installArgs
  } catch {
    Write-Host ""
    Write-Host "Claude Pocket could not be installed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "You can also download it from https://github.com/yuiya-dev/claude-pocket (Code > Download ZIP)," -ForegroundColor Yellow
    Write-Host "unzip it and double-click Install.cmd." -ForegroundColor Yellow
  } finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}
