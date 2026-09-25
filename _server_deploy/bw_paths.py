"""服务器代码共用的三样路径：项目根 / 状态目录 / 用户主目录。

2026-09-25 迁移到 Mac：服务代码里有几十处写死的 "/home/bwicarus/..."（树莓派时代）。
Mac 上 /home 是系统保留的自动挂载点，连目录都建不了，服务一碰就报错退出；
Windows 上这些路径被解析成 C:\\home\\bwicarus\\...，其实也一直是错位的。

规则：
  · 项目根 = 环境变量 CLAUDE_PROJECT；没设时退回树莓派的 /home/bwicarus/claude。
  · 状态目录 = 项目根/state。
  · 主目录 = 当前用户的主目录（树莓派上正好就是 /home/bwicarus）。
所以在树莓派上这三样与原来的写死值完全相同，行为不变。
"""
import os
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT") or "/home/bwicarus/claude")
STATE = PROJECT / "state"
SCRIPTS = PROJECT / "scripts"
HOME = Path.home()
