"""
bwicarus-client launcher

职责：
  1. 读 %LOCALAPPDATA%\\bwicarus-client\\config.json
  2. 首次启动时弹一个最小 GUI 让用户填服务端地址
  3. GET <server>/static/client/manifest.json 拿最新 core 版本
  4. 比对本地 current_core_version，新就下载 core-<ver>.zip 解压到 core/<ver>/
  5. 加载 core/<ver>/main.py:main(config_path) 把控制权交给业务代码

launcher 自身极简，依赖项（customtkinter / requests / ...）由 PyInstaller 打进 .exe，
core 包只含 .py 业务代码，可热更换不需要重新分发 .exe。
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import zipfile
import traceback
import urllib.request
import urllib.error
from pathlib import Path

# 强制 PyInstaller 把 core 运行时所需的所有依赖打进 .exe
# launcher 自身不真正使用这些模块，但删了打包会缺包
import _runtime_imports  # noqa: F401

LAUNCHER_VERSION = "0.2.0"
APP_NAME         = "bwicarus-client"
DEFAULT_SERVER   = "https://bwicarus.space"

# 「数据目录指针」：永远固定在 %APPDATA%\bwicarus-client\datadir.txt
# 内容是用户选定的真实数据目录绝对路径。这是 launcher 唯一硬编码的位置。
def _pointer_file() -> Path:
    base = Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return base / APP_NAME / "datadir.txt"


def _default_data_dir() -> Path:
    base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / APP_NAME


def _read_data_dir_pointer() -> Path | None:
    p = _pointer_file()
    if not p.exists():
        return None
    try:
        line = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line:
        return None
    return Path(line)


def _write_data_dir_pointer(target: Path) -> None:
    p = _pointer_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(target), encoding="utf-8")


def _ask_data_dir_gui(default: Path) -> Path | None:
    """首次启动让用户决定数据目录；返回 None 表示用户取消。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.title(f"{APP_NAME} · 首次安装")
    root.geometry("520x270")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    chosen: dict[str, Path | None] = {"path": None}

    tk.Label(
        root,
        text="选择数据保存位置",
        font=("Microsoft YaHei UI", 14, "bold"),
        anchor="w",
    ).pack(fill="x", padx=20, pady=(20, 4))
    tk.Label(
        root,
        text="客户端会把以下内容存到这里：\n  · 配置文件 config.json\n  · 热更新 core 包\n  · cmd_server 密钥、日志、单例锁",
        justify="left", anchor="w", fg="#555",
    ).pack(fill="x", padx=20, pady=(0, 12))

    var = tk.StringVar(value=str(default))
    row = tk.Frame(root)
    row.pack(fill="x", padx=20, pady=(0, 6))
    tk.Label(row, text="数据目录", width=10, anchor="w").pack(side="left")
    entry = tk.Entry(row, textvariable=var)
    entry.pack(side="left", fill="x", expand=True, padx=(8, 4))

    def pick():
        d = filedialog.askdirectory(title="选择数据目录", parent=root)
        if d:
            var.set(d)

    tk.Button(row, text="…", width=3, command=pick).pack(side="right")
    tk.Label(
        root,
        text="默认在用户配置区下，建议保持。\n如要把数据放到 D 盘 / 网盘 / U 盘，自行选别的目录即可（之后可以从该位置整体迁移）。",
        justify="left", anchor="w", fg="#888", font=("Microsoft YaHei UI", 9),
    ).pack(fill="x", padx=20, pady=(8, 0))

    btn_row = tk.Frame(root)
    btn_row.pack(fill="x", padx=20, pady=18)

    def confirm():
        path = Path(var.get().strip())
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("无法创建", f"创建目录失败：{e}", parent=root)
            return
        chosen["path"] = path
        root.destroy()

    def cancel():
        root.destroy()

    tk.Button(btn_row, text="取消", command=cancel, width=10).pack(side="right", padx=(8, 0))
    tk.Button(
        btn_row, text="确定并继续", command=confirm,
        width=14, bg="#6366f1", fg="#fff", relief="flat",
    ).pack(side="right")

    root.mainloop()
    return chosen["path"]


def app_dir() -> Path:
    """返回当前数据目录。首次启动时若无指针文件，弹窗让用户决定。"""
    pointed = _read_data_dir_pointer()
    if pointed and pointed.exists():
        return pointed
    if pointed and not pointed.exists():
        # 指针指向已不存在的目录（被用户删了 / 移走了）→ 当首次安装重来
        try:
            pointed.mkdir(parents=True, exist_ok=True)
            return pointed
        except Exception:
            pass
    chosen = _ask_data_dir_gui(_default_data_dir())
    if chosen is None:
        # 用户取消 → 退化到默认位置（不写指针，下次还会问）
        d = _default_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d
    _write_data_dir_pointer(chosen)
    return chosen


_APP_DIR    = app_dir()
CONFIG_PATH = _APP_DIR / "config.json"
CORE_ROOT   = _APP_DIR / "core"
LOG_PATH    = _APP_DIR / "launcher.log"
# 让 core 通过环境变量识别同一目录（paths.py 会优先读这个）
os.environ["BWICARUS_APP_DIR"] = str(_APP_DIR)


def log(msg: str) -> None:
    line = f"[{LAUNCHER_VERSION}] {msg}"
    print(line, file=sys.stderr)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"config parse error: {e}")
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def first_run_gui() -> str | None:
    """首次启动让用户填服务端 URL。仅用 stdlib tkinter，不依赖 customtkinter。"""
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    url = simpledialog.askstring(
        f"{APP_NAME} 首次配置",
        "服务端地址：",
        initialvalue=DEFAULT_SERVER,
        parent=root,
    )
    root.destroy()
    if not url:
        return None
    return url.strip().rstrip("/")


def fatal_error(msg: str) -> None:
    log(f"FATAL: {msg}")
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(f"{APP_NAME} 启动失败", msg)
        root.destroy()
    except Exception:
        pass
    sys.exit(1)


def fetch_manifest(server_url: str) -> dict | None:
    url = f"{server_url}/static/client/manifest.json"
    log(f"fetching manifest: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{LAUNCHER_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        log(f"manifest version={data.get('version')}")
        return data
    except urllib.error.URLError as e:
        log(f"manifest fetch failed: {e}")
        return None
    except Exception as e:
        log(f"manifest parse failed: {e}")
        return None


def download_core(server_url: str, manifest: dict) -> Path:
    version  = manifest["version"]
    core_url = manifest["core_url"]
    if not core_url.startswith("http"):
        core_url = server_url + core_url

    target_dir = CORE_ROOT / version
    if target_dir.exists() and (target_dir / "main.py").exists():
        log(f"core {version} already present")
        return target_dir

    CORE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp_zip = CORE_ROOT / f"core-{version}.zip.tmp"
    log(f"downloading core {version} from {core_url}")
    req = urllib.request.Request(core_url, headers={"User-Agent": f"{APP_NAME}/{LAUNCHER_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        tmp_zip.write_bytes(r.read())

    # 解压到 .partial 再 rename，避免半截目录污染
    partial = CORE_ROOT / f"{version}.partial"
    if partial.exists():
        shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    with zipfile.ZipFile(tmp_zip) as zf:
        zf.extractall(partial)
    tmp_zip.unlink(missing_ok=True)

    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    partial.rename(target_dir)
    log(f"core {version} installed at {target_dir}")
    return target_dir


def load_core(core_dir: Path, config_path: Path) -> None:
    log(f"loading core from {core_dir}")
    sys.path.insert(0, str(core_dir))
    import main as core_main  # type: ignore
    if "--qa" in sys.argv:
        log("argv has --qa: launching qa-only mode")
        core_main.main_qa_only(config_path=str(config_path))
    else:
        core_main.main(config_path=str(config_path))


# 单例锁全局引用：进程退出时 OS 关闭文件 = 释放锁
_INSTANCE_LOCK = None


def acquire_instance_lock() -> bool:
    """主 GUI 模式独占锁：同一时刻只能一个 client 主窗口。
    返回 True 表示成功获取锁；False 表示已有实例在跑。
    --qa 模式（快捷方式）跳过该锁，允许并发跑短任务。
    """
    global _INSTANCE_LOCK
    if "--qa" in sys.argv:
        return True
    if os.name != "nt":
        return True
    try:
        import msvcrt
        lock_path = app_dir() / "client.lock"
        _INSTANCE_LOCK = open(lock_path, "a+b")
        msvcrt.locking(_INSTANCE_LOCK.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False
    except Exception as e:
        log(f"instance lock skipped: {e}")
        return True


def main() -> None:
    if not acquire_instance_lock():
        log("another client instance is already running; aborting")
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(
                APP_NAME,
                "客户端已经在运行了。\n\n请到系统托盘找紫色 B 图标，\n双击或右键 → 显示主窗口。",
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    cfg = load_config()
    server_url = cfg.get("server_url")
    if not server_url:
        server_url = first_run_gui()
        if not server_url:
            log("user cancelled first-run setup")
            sys.exit(0)
        cfg["server_url"] = server_url
        save_config(cfg)

    manifest = fetch_manifest(server_url)
    current_version = cfg.get("current_core_version")

    target_dir: Path | None = None
    if manifest:
        latest = manifest.get("version")
        if latest and latest != current_version:
            try:
                target_dir = download_core(server_url, manifest)
                cfg["current_core_version"] = latest
                save_config(cfg)
            except Exception as e:
                log(f"download failed: {e}\n{traceback.format_exc()}")
                if current_version:
                    target_dir = CORE_ROOT / current_version
        else:
            target_dir = CORE_ROOT / latest if latest else None
    elif current_version:
        # 离线：用本地版
        target_dir = CORE_ROOT / current_version

    if not target_dir or not target_dir.exists() or not (target_dir / "main.py").exists():
        fatal_error(
            f"无法获取 core 包。\n服务端: {server_url}\n"
            f"日志: {LOG_PATH}\n请检查网络或服务端配置。"
        )

    if os.getenv("BWICARUS_LAUNCHER_DRYRUN") == "1":
        log(f"DRYRUN OK, core ready at {target_dir}")
        sys.exit(0)

    try:
        load_core(target_dir, CONFIG_PATH)
    except Exception as e:
        log(f"core crashed: {e}\n{traceback.format_exc()}")
        fatal_error(f"core 运行出错：{e}\n详情见 {LOG_PATH}")


if __name__ == "__main__":
    main()
