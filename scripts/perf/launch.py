"""
启动 MSI Afterburner 并确认已开始记录。

用法：
  python launch.py
"""

import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

AB_EXE = r"C:\Program Files (x86)\MSI Afterburner\MSIAfterburner.exe"
HML_PATH = r"C:\Program Files (x86)\MSI Afterburner\HardwareMonitoring.hml"


def is_running(name: str) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="replace"
    )
    return name.lower() in result.stdout.lower()


def main():
    if is_running("MSIAfterburner.exe"):
        print("MSI Afterburner 已在运行，正在记录数据。")
    else:
        print("启动 MSI Afterburner...")
        subprocess.Popen([AB_EXE])
        time.sleep(4)
        if is_running("MSIAfterburner.exe"):
            print("MSI Afterburner 已启动。")
        else:
            print(f"[!] 启动失败，请手动打开：{AB_EXE}")
            sys.exit(1)

    print(f"\nHML 日志路径：{HML_PATH}")
    print("现在可以开始游戏，结束后运行 analyze.py 进行分析。")


if __name__ == "__main__":
    main()
