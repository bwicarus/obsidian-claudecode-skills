import json
import subprocess
import sys
import urllib.request

PYTHON = r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"
SCRIPTS = r"C:\claude\scripts"
DASH = r"C:\claude\dashboard"
HIST = r"C:\claude\history"
SERVER = r"root@31.220.31.30"
DASH_DEST = SERVER + r":/root/webapp/data/dashboard/"
HIST_DEST = SERVER + r":/root/webapp/data/history/"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

sys.path.insert(0, SCRIPTS)
from task_tracker import track


def anki_running() -> bool:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8765",
            data=b'{"action":"version","version":6}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read()).get("error") is None
    except Exception:
        return False


def run_step(cmd: list) -> int:
    r = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW,
    )
    return r.returncode


def main():
    running = anki_running()
    wait = 0 if running else 120

    steps = [
        ("更新 Anki 学习状态", [
            PYTHON, rf"{SCRIPTS}\anki_status.py",
            "--all", "--write-frontmatter", "--write-record",
            "--wait-seconds", str(wait),
        ]),
        ("计算复习优先级", [
            PYTHON, rf"{SCRIPTS}\review_priority.py",
            "--write-frontmatter", "--write-record",
        ]),
        ("生成 dashboard.json", [
            PYTHON, rf"{SCRIPTS}\export_dashboard.py",
        ]),
        ("推送仪表板到服务器", [
            "scp",
            rf"{DASH}\index.html",
            rf"{DASH}\dashboard.json",
            DASH_DEST,
        ]),
        ("导出问答历史", [
            PYTHON, rf"{SCRIPTS}\export_history.py",
        ]),
        ("推送到服务器", [
            "scp",
            rf"{HIST}\index.html",
            rf"{HIST}\history.json",
            HIST_DEST,
        ]),
        ("推送历史图片", [
            "scp", "-r",
            rf"{HIST}\images",
            HIST_DEST,
        ]),
    ]

    import time as _time
    total = len(steps)
    overall_start = _time.time()
    with track("上传网页") as task:
        task.set_progress(0, total)
        for i, (label, cmd) in enumerate(steps, 1):
            task.update(f"[{i}/{total}] {label}")
            t0 = _time.time()
            rc = run_step(cmd)
            elapsed = int(_time.time() - t0)
            if rc != 0:
                task.log(f"✗ {label} (退出码 {rc})")
                task.update(f"失败：{label}")
                task.set_summary(f"在「{label}」失败（退出码 {rc}）")
                sys.exit(rc)
            task.log(f"✓ {label} ({elapsed}s)")
            task.set_progress(i, total)
        total_elapsed = int(_time.time() - overall_start)
        summary = f"成功推送 {total}/{total} 步  总耗时 {total_elapsed}s"
        task.update(summary)
        task.set_summary(summary)


if __name__ == "__main__":
    main()
