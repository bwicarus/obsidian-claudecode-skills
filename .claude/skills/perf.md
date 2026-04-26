# Skill: perf

采集游戏运行时的硬件数据，分析帧率波动的主要影响因素。

## 触发方式
用户输入 `/perf`，可附加意图：
- `/perf 开始` — 启动 Afterburner，准备采集
- `/perf 分析` — 分析 HML 日志，输出相关性报告
- `/perf 低帧` — 找出帧率跌破阈值的原因

## 依赖工具
- MSI Afterburner（已安装：`C:\Program Files (x86)\MSI Afterburner\MSIAfterburner.exe`）
- Python 依赖：pandas, scipy, scikit-learn（已安装）
- HML 日志路径：`C:\Program Files (x86)\MSI Afterburner\HardwareMonitoring.hml`

## 脚本目录
`C:\obsidian\claude\scripts\perf\`
- `launch.py`  — 启动 Afterburner，确认已开始记录
- `analyze.py` — 解析 HML，输出相关性分析和回归报告
- `drops.py`   — 找出低帧事件，给出逐次诊断原因

---

## 执行流程

### 阶段一：开始采集
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe C:\obsidian\claude\scripts\perf\launch.py
```
确认 Afterburner 已启动后，告知用户可以开始游戏。

### 阶段二：等待
用户游戏期间无需操作。等用户说「测试完了」或「分析」。

### 阶段三：综合分析
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe C:\obsidian\claude\scripts\perf\analyze.py ^
  --hml "C:\Program Files (x86)\MSI Afterburner\HardwareMonitoring.hml" ^
  --output "C:\Users\bwica\Desktop\report.html"
```
可加 `--start HH:MM:SS --end HH:MM:SS` 只分析游戏期间的数据段。

### 阶段四：低帧诊断（用户询问时）
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe C:\obsidian\claude\scripts\perf\drops.py ^
  --hml "C:\Program Files (x86)\MSI Afterburner\HardwareMonitoring.hml" ^
  --threshold 50
```
可加 `--start / --end` 限定时间范围。

---

## 输出说明
- `report.html` 保存到桌面，包含相关性表格和逐秒数据
- 终端直接输出 Top 15 相关指标 + 多元回归 R² 和标准化系数
- drops.py 输出每次低帧事件的原因诊断（GPU 降频 / 显存压力 / 核显介入等）

## 常见原因速查
| 现象 | 原因 |
|------|------|
| GPU1 功耗骤降至 <50W | 游戏切换/加载界面/窗口失焦 |
| 显存占用突增 | 新场景/资产加载，显存带宽争用 |
| GPU2（核显）使用率 >10% | 混合显卡切换，独显卸载 |
| GPU 功耗正常但帧率低 | 场景复杂度超出显卡能力，CPU 瓶颈 |
