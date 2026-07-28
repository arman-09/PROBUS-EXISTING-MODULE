"""
TPNODL Monitor -- System Tray Launcher
Starts app.py as a background subprocess, shows tray icon.
Writes PID file so stop/start can cleanly kill previous instances
without needing Administrator rights.
Requirements: pip install pystray pillow
"""

import sys, os, threading, time, webbrowser, subprocess

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PID_FILE   = os.path.join(BASE_DIR, "data", ".tpnodl.pid")
PORT_FILE  = os.path.join(BASE_DIR, "data", ".port")
APP_SCRIPT = os.path.join(BASE_DIR, "app.py")
PREFERRED_PORT = 7777

def _read_port():
    """Read port chosen by find_free_port.py."""
    try:
        return int(open(PORT_FILE).read().strip())
    except Exception:
        return PREFERRED_PORT

PORT = _read_port()

os.chdir(BASE_DIR)

# ── Hide console ───────────────────────────────────────────────────────────
def hide_console():
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(
            ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass

hide_console()

# ── Write PID file ─────────────────────────────────────────────────────────
def write_pid(app_pid):
    try:
        os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(f"{os.getpid()}\n{app_pid}\n")
    except Exception as e:
        print(f"PID file write failed: {e}")

def clear_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass

# ── Tray icon ──────────────────────────────────────────────────────────────
def make_icon(running=True):
    from PIL import Image, ImageDraw
    size = 64
    img  = Image.new("RGB", (size, size), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    if running:
        draw.ellipse([2, 2, size-2, size-2], fill=(15, 20, 50))
        bolt = [(32,8),(18,34),(30,34),(20,56),(46,28),(33,28),(44,8)]
        draw.polygon(bolt, fill=(0, 210, 255))
        draw.ellipse([2, 2, size-2, size-2], outline=(0, 180, 220), width=2)
    else:
        draw.ellipse([2, 2, size-2, size-2], fill=(80, 15, 15))
        draw.line([16, 16, 48, 48], fill=(255, 80, 80), width=6)
        draw.line([48, 16, 16, 48], fill=(255, 80, 80), width=6)
        draw.ellipse([2, 2, size-2, size-2], outline=(180, 40, 40), width=2)
    return img

# ── Subprocess management ──────────────────────────────────────────────────
_proc = None

def start_app():
    global _proc
    _proc = subprocess.Popen(
        [sys.executable, APP_SCRIPT],
        cwd=BASE_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    write_pid(_proc.pid)
    return _proc

def stop_app():
    global _proc
    port = _read_port()

    # Step 1: Ask Flask to shut down gracefully via /api/shutdown
    # This closes sockets cleanly — avoids TIME_WAIT/CLOSE_WAIT buildup
    try:
        import urllib.request as _ur
        _ur.urlopen(f"http://127.0.0.1:{port}/api/shutdown", timeout=3)
    except Exception:
        pass  # Endpoint may not exist or already dead — that's fine

    time.sleep(1)  # Give Flask time to close sockets

    # Step 2: Terminate subprocess handle
    if _proc and _proc.poll() is None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass

    # Step 3: Kill by PID file
    try:
        import subprocess as _sp
        _sp.run(
            [sys.executable, os.path.join(BASE_DIR, "kill_prev.py")],
            cwd=BASE_DIR, capture_output=True, timeout=8
        )
    except Exception:
        pass

    clear_pid()

# ── Tray actions ───────────────────────────────────────────────────────────
def stop_server(icon=None, item=None):
    if icon:
        try:
            icon.icon  = make_icon(running=False)
            icon.title = "TPNODL Monitor — Stopped"
        except Exception:
            pass
        try:
            icon.notify("TPNODL Monitor stopped.", "TPNODL")
        except Exception:
            pass
    stop_app()
    def _exit():
        time.sleep(1.2)
        if icon:
            try: icon.stop()
            except Exception: pass
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()

def open_browser(icon=None, item=None):
    port = _read_port()
    webbrowser.open(f"http://127.0.0.1:{port}")

def show_logs(icon=None, item=None):
    log_dir = os.path.join(BASE_DIR, "logs")
    os.startfile(log_dir if os.path.isdir(log_dir) else BASE_DIR)

def restart_server(icon=None, item=None):
    if icon:
        try: icon.notify("Restarting TPNODL Monitor...", "TPNODL")
        except Exception: pass
    stop_app()
    time.sleep(2)
    start_app()
    if icon:
        try:
            icon.icon  = make_icon(running=True)
            icon.title = f"TPNODL Monitor — Running (:{PORT})"
        except Exception: pass

def watchdog(icon):
    # Wait for app.py to fully start and settle on a port
    time.sleep(8)

    # Re-read actual port (app.py may have switched from preferred)
    actual_port = _read_port()
    try:
        icon.title = f"TPNODL Monitor — Running (:{actual_port})"
    except Exception:
        pass

    while True:
        time.sleep(15)
        # Check if process is alive
        if _proc and _proc.poll() is None:
            continue  # Still running — all good

        # Process appears dead — verify by checking HTTP response
        # (avoids false alarm when process was intentionally restarted)
        import urllib.request as _ur
        port = _read_port()
        try:
            _ur.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3)
            # HTTP responded — a new process took over, update our reference
            continue
        except Exception:
            pass

        # Truly dead — server is not responding
        try:
            icon.title = "TPNODL Monitor — CRASHED"
            icon.icon  = make_icon(running=False)
            icon.notify(
                f"⚠ TPNODL Monitor stopped responding!\n"
                f"Right-click tray icon → Restart Server",
                "TPNODL Monitor"
            )
        except Exception:
            pass
        break

# ── Run tray ───────────────────────────────────────────────────────────────
def run_tray():
    try:
        import pystray
        from pystray import MenuItem as Item, Menu
    except ImportError:
        print("pystray not installed. Run: pip install pystray pillow")
        time.sleep(99999)
        return

    try:
        img = make_icon(running=True)
    except Exception:
        from PIL import Image
        img = Image.new("RGB", (64, 64), color=(0, 100, 200))

    menu = Menu(
        Item("📊 Open Dashboard",  open_browser, default=True),
        Menu.SEPARATOR,
        Item("⏹  Stop Server",     lambda icon, item: stop_server(icon, item)),
        Item("🔄 Restart Server",  restart_server),
        Menu.SEPARATOR,
        Item("📁 View Logs",       show_logs),
        Menu.SEPARATOR,
        Item("❌ Quit",            lambda icon, item: stop_server(icon, item)),
    )

    icon = pystray.Icon(
        name  = "TPNODL_Monitor",
        icon  = img,
        title = f"TPNODL Monitor — Running (:{PORT})",
        menu  = menu,
    )

    def notify_ready():
        time.sleep(6)  # wait for app.py to bind and update port file
        actual_port = _read_port()
        try:
            icon.notify(
                f"TPNODL Monitor running\n"
                f"Dashboard: http://127.0.0.1:{actual_port}\n"
                f"Right-click icon to Stop/Restart",
                "TPNODL Monitor"
            )
        except Exception: pass

    threading.Thread(target=notify_ready,          daemon=True).start()
    threading.Thread(target=watchdog, args=(icon,), daemon=True).start()
    icon.run()

# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    start_app()
    run_tray()
