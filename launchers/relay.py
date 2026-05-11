"""
Persistent SSH reverse tunnel for screenshot QA.

Forwards bwicarus.space:5001 to the local remote-QA server at 127.0.0.1:5001.
If another matching ssh tunnel already exists, this process waits quietly.
"""

import subprocess
import time
from pathlib import Path

SSH_KEY = Path.home() / ".ssh" / "id_ed25519"
REMOTE = "root@bwicarus.space"
REMOTE_PORT = 5001
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 5001
RESTART_DELAY = 30
TUNNEL_MATCH = "-R 5001:127.0.0.1:5001 root@bwicarus.space"


def tunnel_cmd() -> list[str]:
    return [
        "ssh.exe",
        "-i",
        str(SSH_KEY),
        "-N",
        "-R",
        f"{REMOTE_PORT}:{LOCAL_HOST}:{LOCAL_PORT}",
        REMOTE,
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ExitOnForwardFailure=yes",
    ]


def existing_tunnel_running() -> bool:
    ps = (
        "$m = '" + TUNNEL_MATCH.replace("'", "''") + "'; "
        "$p = Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'ssh.exe' -and $_.CommandLine -like ('*' + $m + '*') }; "
        "if ($p) { exit 0 } else { exit 1 }"
    )
    try:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
    except Exception:
        return False


def main():
    while True:
        if existing_tunnel_running():
            print(f"[relay] existing tunnel detected; rechecking in {RESTART_DELAY}s", flush=True)
            time.sleep(RESTART_DELAY)
            continue
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[relay] starting tunnel at {started}: {REMOTE_PORT} -> {LOCAL_HOST}:{LOCAL_PORT}", flush=True)
        try:
            rc = subprocess.run(tunnel_cmd()).returncode
            print(f"[relay] tunnel exited with code {rc}; restarting in {RESTART_DELAY}s", flush=True)
        except KeyboardInterrupt:
            print("[relay] stopped", flush=True)
            return
        except Exception as exc:
            print(f"[relay] error: {exc}; restarting in {RESTART_DELAY}s", flush=True)
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
