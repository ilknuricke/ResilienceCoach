"""
Keeps the Resilience Sense server running: starts server.py without a console window,
restarts it a few seconds after any exit, and logs to data/server.log.

Started by start.bat / start-lan.bat (via pythonw, so there's no window to close by
accident). Stopped by stop.bat. If the server dies mid-session it comes back and
resumes the session (see restore_runstate in server.py).
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
LOG = DATA / "server.log"
PIDFILE = DATA / "supervisor.pid"
PORT = int(os.environ.get("PORT", 8765))
PY = HERE / ".venv" / "Scripts" / "python.exe"
ARGS = [str(PY), str(HERE / "server.py")] + [a for a in sys.argv[1:] if a == "--lan"]
CREATE_NO_WINDOW = 0x08000000


def port_busy() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def log(msg: str):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [supervisor] {msg}\n")


def main():
    if port_busy():
        log(f"port {PORT} already in use; another instance is running. Exiting.")
        return
    PIDFILE.write_text(str(os.getpid()))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    fails = 0
    while True:
        if LOG.exists() and LOG.stat().st_size > 20_000_000:      # keep the log bounded
            LOG.replace(LOG.with_suffix(".old.log"))
        log("starting server")
        started = time.time()
        with open(LOG, "a", encoding="utf-8") as out:
            proc = subprocess.Popen(ARGS, cwd=HERE, stdout=out, stderr=subprocess.STDOUT,
                                    env=env, creationflags=CREATE_NO_WINDOW)
            code = proc.wait()
        if (DATA / "stop.flag").exists():
            (DATA / "stop.flag").unlink(missing_ok=True)
            log("stop requested; supervisor exiting")
            break
        fails = fails + 1 if time.time() - started < 30 else 0
        delay = min(30, 3 * (fails + 1))       # back off if it keeps dying at startup
        log(f"server exited with code {code}; restarting in {delay} s")
        time.sleep(delay)
    PIDFILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
