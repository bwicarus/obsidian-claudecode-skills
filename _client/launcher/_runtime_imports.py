"""PyInstaller 静态分析提示文件。

launcher.py 自身不直接用 core 里那些依赖（customtkinter / watchdog / pystray / PIL / 各种 stdlib），
所以打 .exe 时 PyInstaller 检索不到这些模块就不会打进去，core 在运行时 import 就 ModuleNotFoundError。

解决：launcher.py 一行 `from . import _runtime_imports`（或裸 import），让所有依赖在静态分析时被发现。
本文件不被实际调用，只起"清单"作用。core/*.py 新增 import 时同步往这里添。

维护：每次 core 新增 pip 依赖（含子模块）或非 launcher.py 用过的 stdlib，都要在这里 import 一次。
"""
# ── stdlib（core/api_client.py / uploader.py / runner.py / ai_backends.py / qa_browser.py 等用到） ──
import uuid             # noqa: F401
import mimetypes        # noqa: F401
import subprocess       # noqa: F401
import threading        # noqa: F401
import urllib.request   # noqa: F401
import urllib.parse     # noqa: F401
import urllib.error     # noqa: F401
import sqlite3          # noqa: F401
import hashlib          # noqa: F401
import ctypes           # noqa: F401
import socket           # noqa: F401
import webbrowser       # noqa: F401
import shutil           # noqa: F401
import base64           # noqa: F401
import io               # noqa: F401
import http.server      # noqa: F401
import socketserver     # noqa: F401
import datetime         # noqa: F401

# ── tkinter 子模块（core/gui.py 通过 from tkinter import filedialog/messagebox 用到） ──
import tkinter.filedialog   # noqa: F401
import tkinter.messagebox   # noqa: F401
import tkinter.simpledialog # noqa: F401
import tkinter.ttk          # noqa: F401

# ── pip 依赖（core 模块直接 import 的） ──
import customtkinter        # noqa: F401
import pystray              # noqa: F401
import pystray._win32       # noqa: F401  # Windows backend 显式
import watchdog             # noqa: F401
import watchdog.observers   # noqa: F401
import watchdog.events      # noqa: F401
import PIL                  # noqa: F401
import PIL.Image            # noqa: F401
import PIL.ImageDraw        # noqa: F401
import PIL.ImageFont        # noqa: F401
import keyboard             # noqa: F401  # 全局热键
