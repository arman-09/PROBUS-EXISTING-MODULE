"""
install_service.py — Install TPNODL Monitor as a Windows Service
=================================================================
Uses pywin32. Run once as Administrator:
    python install_service.py install
    python install_service.py start
    python install_service.py stop
    python install_service.py remove
"""

import sys, os, subprocess

SERVICE_NAME    = "TPNODLMonitor"
SERVICE_DISPLAY = "TPNODL Load & Voltage Monitor"
SERVICE_DESC    = "Realtime scraper, violation detector, and notification server for TPNODL PSCC."

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON   = sys.executable
SCRIPT   = os.path.join(BASE_DIR, "app.py")


def _run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print("STDERR:", result.stderr)
    return result.returncode == 0


# ─── Try pywin32 first ────────────────────────────────────
def install_win32():
    try:
        import win32serviceutil, win32service, win32event, servicemanager
        import socket

        class TPNODLService(win32serviceutil.ServiceFramework):
            _svc_name_        = SERVICE_NAME
            _svc_display_name_= SERVICE_DISPLAY
            _svc_description_ = SERVICE_DESC

            def __init__(self, args):
                win32serviceutil.ServiceFramework.__init__(self, args)
                self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
                socket.setdefaulttimeout(60)
                self._proc = None

            def SvcStop(self):
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
                win32event.SetEvent(self.hWaitStop)
                if self._proc:
                    self._proc.terminate()

            def SvcDoRun(self):
                servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
                os.chdir(BASE_DIR)
                self._proc = subprocess.Popen(
                    [PYTHON, SCRIPT],
                    cwd=BASE_DIR,
                    stdout=open(os.path.join(BASE_DIR,"logs","service_stdout.log"),"a"),
                    stderr=open(os.path.join(BASE_DIR,"logs","service_stderr.log"),"a"),
                )
                win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

        win32serviceutil.HandleCommandLine(TPNODLService)
        return True
    except ImportError:
        return False


# ─── NSSM fallback ───────────────────────────────────────
def install_nssm():
    nssm = _find_nssm()
    if not nssm:
        print("NSSM not found. Download from https://nssm.cc and add to PATH, or install pywin32.")
        return False
    action = sys.argv[1] if len(sys.argv) > 1 else "install"
    if action == "install":
        _run([nssm, "install", SERVICE_NAME, PYTHON, SCRIPT])
        _run([nssm, "set", SERVICE_NAME, "AppDirectory", BASE_DIR])
        _run([nssm, "set", SERVICE_NAME, "DisplayName", SERVICE_DISPLAY])
        _run([nssm, "set", SERVICE_NAME, "Description", SERVICE_DESC])
        _run([nssm, "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"])
        _run([nssm, "set", SERVICE_NAME, "AppStdout",
              os.path.join(BASE_DIR,"logs","nssm_stdout.log")])
        _run([nssm, "set", SERVICE_NAME, "AppStderr",
              os.path.join(BASE_DIR,"logs","nssm_stderr.log")])
        print(f"Service '{SERVICE_NAME}' installed via NSSM.")
    elif action == "start":
        _run([nssm, "start", SERVICE_NAME])
    elif action == "stop":
        _run([nssm, "stop", SERVICE_NAME])
    elif action == "remove":
        _run([nssm, "remove", SERVICE_NAME, "confirm"])
    return True


def _find_nssm():
    for path in (["nssm"], [r"C:\nssm\win64\nssm.exe"], [r"C:\tools\nssm.exe"]):
        try:
            subprocess.run(path + ["version"], capture_output=True)
            return path[0]
        except FileNotFoundError:
            continue
    return None


if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    if len(sys.argv) < 2:
        print(f"Usage: python install_service.py [install|start|stop|remove]")
        sys.exit(1)
    if not install_win32():
        install_nssm()
