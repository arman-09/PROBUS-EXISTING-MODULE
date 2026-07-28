"""
kill_prev.py — Kill previous TPNODL Monitor instances.
Works without admin rights by targeting own-user processes only.
Three methods (all attempted):
  1. PID file  — fastest, most precise
  2. Port file — kills whatever is on the saved port
  3. Name scan — fallback, matches app.py / tray.pyw by username
"""
import os, sys, signal, subprocess

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
PID_FILE  = os.path.join(BASE_DIR, "data", ".tpnodl.pid")
PORT_FILE = os.path.join(BASE_DIR, "data", ".port")
MY_PID    = os.getpid()

def kill_pid(pid, label=""):
    if pid == MY_PID:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"  Killed PID {pid} {label}")
    except PermissionError:
        print(f"  PID {pid} — no permission (not our process)")
    except ProcessLookupError:
        pass  # Already dead

def method1_pid_file():
    """Kill PIDs saved in .tpnodl.pid"""
    if not os.path.exists(PID_FILE):
        return
    try:
        pids = [int(x) for x in open(PID_FILE).read().split() if x.strip().isdigit()]
        for pid in pids:
            kill_pid(pid, "(from PID file)")
        os.remove(PID_FILE)
    except Exception as e:
        print(f"  PID file method: {e}")

def method2_port_file():
    """Kill whatever process is listening on our saved port."""
    if not os.path.exists(PORT_FILE):
        return
    try:
        port = int(open(PORT_FILE).read().strip())
        result = subprocess.run(
            f'netstat -ano 2>nul | findstr " :{port} " | findstr "LISTENING"',
            shell=True, capture_output=True, text=True
        )
        pids_seen = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts:
                try:
                    pid = int(parts[-1])
                    if pid not in pids_seen:
                        pids_seen.add(pid)
                        kill_pid(pid, f"(port {port})")
                except Exception:
                    pass
    except Exception as e:
        print(f"  Port file method: {e}")

def method3_name_scan():
    """Find app.py and tray.pyw processes belonging to current user."""
    try:
        username = os.environ.get("USERNAME", "")
        if not username:
            return
        result = subprocess.run(
            ["wmic", "process", "get", "processid,commandline"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if ("app.py" in line or "tray.pyw" in line) and "kill_prev" not in line:
                parts = line.split()
                if parts:
                    try:
                        pid = int(parts[-1])
                        kill_pid(pid, f"(name scan: {line[:60]})")
                    except Exception:
                        pass
    except Exception as e:
        print(f"  Name scan method: {e}")

if __name__ == "__main__":
    print("Stopping previous TPNODL Monitor instances...")
    method1_pid_file()
    method2_port_file()
    method3_name_scan()
    print("  Cleanup complete.")
