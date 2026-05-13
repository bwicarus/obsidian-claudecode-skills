"""
bwicarus-client core (业务入口)

launcher 调用 main(config_path)。core 负责：
  - 加载配置
  - 启动主窗口
  - 提供"上传 dashboard / 改配置 / 改 token"等业务功能

热更新机制：launcher 每次启动时检查服务端 manifest，新版自动下载并切换到新 core。
"""
from __future__ import annotations

CORE_VERSION = "0.9.32"

import json
import sys
from pathlib import Path


def main(config_path: str) -> None:
    """launcher 入口。传入 config.json 绝对路径。"""
    cfg_path = Path(config_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}

    # 把 core 自身目录加入 sys.path（防止 launcher 没加全）
    sys.path.insert(0, str(Path(__file__).parent))

    from gui import MainWindow  # type: ignore
    win = MainWindow(cfg_path, cfg, core_version=CORE_VERSION)
    win.run()


def main_qa_only(config_path: str) -> None:
    """快捷方式入口：跳过主 GUI，直接触发一次截图问答。

    launcher 检测到 --qa 参数时调用此函数。
    """
    cfg_path = Path(config_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    sys.path.insert(0, str(Path(__file__).parent))
    from qa_browser import launch as launch_qa  # type: ignore
    launch_qa(get_cfg=lambda: cfg)
