"""The office as an app: an icon in the Windows notification area, and a window of its own.

The server starts the icon and the icon leaves with it. Clicking the icon brings the office's window
back (or opens one); closing that window changes nothing, the office keeps running. The window is the
page in Chrome's or Edge's app mode: no tabs or address bar, its own entry on the taskbar.
"""
import base64
import os
import subprocess
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(ROOT, "icon.png")
BROWSERS = (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",  # Chrome when it is installed, else Edge
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")
ALARM_TITLE = "⏰ Meeting soon!"  # what the page's title flashes to while a meeting reminder is due

# Left click opens the office; the menu has Open and Quit. It exits when the server is gone, so a
# server that was killed never leaves its icon behind.
TRAY = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
function Call-Office($path) {
  try {
    $c = New-Object System.Net.WebClient
    $c.Proxy = $null
    $c.Headers['Content-Type'] = 'application/json'
    [void]$c.UploadString('http://127.0.0.1:' + $env:OFFICE_PORT + $path, '{}')
  } catch {}
}
$n = New-Object System.Windows.Forms.NotifyIcon
try {
  $src = [System.Drawing.Image]::FromFile($env:OFFICE_ICON)
  $bmp = New-Object System.Drawing.Bitmap 32, 32
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.InterpolationMode = 'HighQualityBicubic'
  $g.DrawImage($src, 0, 0, 32, 32)
  $g.Dispose(); $src.Dispose()
  $n.Icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
} catch { $n.Icon = [System.Drawing.SystemIcons]::Application }
$n.Text = $env:OFFICE_NAME
$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add('Open ' + $env:OFFICE_NAME, $null, { Call-Office '/show' })
[void]$menu.Items.Add('Quit', $null, { Call-Office '/quit'; [System.Windows.Forms.Application]::Exit() })
$n.ContextMenuStrip = $menu
$n.Add_MouseClick({ if ($_.Button -eq 'Left') { Call-Office '/show' } })
$n.Visible = $true
$t = New-Object System.Windows.Forms.Timer
$t.Interval = 2000
$t.Add_Tick({ if (-not (Get-Process -Id $env:OFFICE_PID -ErrorAction SilentlyContinue)) { [System.Windows.Forms.Application]::Exit() } })
$t.Start()
[System.Windows.Forms.Application]::Run()
$n.Visible = $false
$n.Dispose()
"""


def start(name, port):
    """Puts the office's icon in the notification area (Windows only)."""
    if os.name != "nt":
        return
    env = dict(os.environ, OFFICE_NAME=name, OFFICE_PORT=str(port), OFFICE_PID=str(os.getpid()), OFFICE_ICON=ICON)
    try:
        code = base64.b64encode(TRAY.encode("utf-16-le")).decode()
        subprocess.Popen(["powershell", "-NoProfile", "-STA", "-EncodedCommand", code], env=env, creationflags=0x08000000)
    except OSError:
        pass


def window(name):
    """Handle of the office's own window, or None. An app window's title is the page's title alone,
    which ends in the office's name; a browser tab showing the office has the browser's name after it."""
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.GetWindowTextLengthW.argtypes = u.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        n = u.GetWindowTextLengthW(h)
        if not n or not u.IsWindowVisible(h) or u.GetWindow(h, 4):  # visible, titled, unowned
            return True
        title = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, title, n + 1)
        if not title.value.endswith(name) and title.value != ALARM_TITLE:
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        hp = k.OpenProcess(0x1000, False, pid.value)
        if hp:
            nb, size = ctypes.create_unicode_buffer(400), wintypes.DWORD(400)
            ok = k.QueryFullProcessImageNameW(hp, 0, nb, ctypes.byref(size))
            k.CloseHandle(hp)
            if ok and os.path.basename(nb.value).lower() in ("msedge.exe", "chrome.exe"):
                found.append(h)
        return True

    u.EnumWindows(each, 0)
    return found[0] if found else None


showing = threading.Lock()  # one click at a time, so two quick clicks open one window


def front(h):
    """Brings a window to the front. Returns what happened."""
    import ctypes
    u = ctypes.windll.user32
    u.keybd_event(0x12, 0, 0, 0)  # a tap of Alt: Windows lets a background program raise a
    u.keybd_event(0x12, 0, 2, 0)  # window only just after a key press
    if u.IsIconic(h):
        u.ShowWindow(h, 9)  # restore a minimized window
    return "raised" if u.SetForegroundWindow(h) else "refused"


def show(name, port):
    """Brings the office's window to the front, opening one when there is none. Returns what it did."""
    if os.name != "nt":  # macOS: no tray icon or app window of our own; the default browser shows it
        try:
            subprocess.Popen(["open", "http://localhost:%d/" % port])
            return "browser"
        except OSError:
            return "no browser"
    with showing:
        h = window(name)
        if h:
            return front(h)
        url = "http://localhost:%d/" % port
        exe = next((p for p in map(os.path.expandvars, BROWSERS) if os.path.isfile(p)), None)
        if not exe:
            os.startfile(url)  # neither Chrome nor Edge: an ordinary tab in the default browser
            return "browser"
        # Its own browser profile, outside the office's folder (which may be synced): the window is
        # then a program of its own, and may play the desk sounds before the page has been clicked.
        profile = os.path.join(os.environ.get("LOCALAPPDATA") or ROOT, name, "window")
        args = [exe, "--app=" + url, "--user-data-dir=" + profile, "--no-first-run",
                "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required"]
        if not os.path.isdir(profile):
            # The first time, fill the screen: a new window is small enough for the page to fall back
            # to its compact layout. After that the browser reopens it where it was left.
            args.append("--start-maximized")
        subprocess.Popen(args)
        for _ in range(40):  # a window opened from the background can come up behind the others
            time.sleep(0.25)
            h = window(name)
            if h:
                return "opened, " + front(h)
        return "opened"
