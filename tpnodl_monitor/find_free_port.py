"""
find_free_port.py — Find a truly free port using TCP connect test.
Uses connect() not bind() — SO_REUSEADDR on bind() gives false positives.
Logic: if we can CONNECT to the port, something is listening = port busy.
       If connect is REFUSED, port is free.
"""
import socket, os, sys, subprocess

PREFERRED  = 7777
SCAN_START = 7700
SCAN_END   = 7850
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PORT_FILE  = os.path.join(BASE_DIR, "data", ".port")

def is_port_in_use(port):
    """
    Returns True if something is actively listening on the port.
    Uses TCP connect — reliable even with ghost SO_REUSEADDR sockets.
    """
    # Method 1: netstat check (most reliable on Windows)
    try:
        result = subprocess.run(
            f'netstat -ano 2>nul | findstr " :{port} " | findstr "LISTENING"',
            shell=True, capture_output=True, text=True, timeout=3
        )
        if result.stdout.strip():
            return True  # Something is LISTENING
    except Exception:
        pass

    # Method 2: Try TCP connect as fallback
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            result = s.connect_ex(("127.0.0.1", port))
            if result == 0:
                return True  # Connected = something listening
    except Exception:
        pass

    return False  # Nothing listening = port is free

def find_port():
    # Try preferred first
    if not is_port_in_use(PREFERRED):
        return PREFERRED
    print(f"  Port {PREFERRED} is busy, scanning {SCAN_START}-{SCAN_END}...",
          file=sys.stderr)
    for port in range(SCAN_START, SCAN_END + 1):
        if port == PREFERRED:
            continue
        if not is_port_in_use(port):
            print(f"  Found free port: {port}", file=sys.stderr)
            return port
    raise RuntimeError(f"No free port found in range {SCAN_START}-{SCAN_END}")

if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    port = find_port()
    with open(PORT_FILE, "w") as f:
        f.write(str(port))
    print(port)  # stdout only — captured by start.bat
