@echo off
REM 启动 PC 常驻公式 OCR 服务(给阅读器书架「🧮 本地公式 OCR」按钮用)。双击运行或加进开机启动。
set "VPIX=C:\Users\bwica\formula-ocr\venv-pix\Scripts\python.exe"
set "SRV=C:\Users\bwica\formula-ocr\formula_ocr_server.py"
title formula-ocr-server :8765
"%VPIX%" "%SRV%"
pause
