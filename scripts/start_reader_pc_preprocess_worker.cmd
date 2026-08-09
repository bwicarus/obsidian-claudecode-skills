@echo off
setlocal
set "BW_PROJECT=%~dp0.."
if not defined BW_READER_PC_OCR_BASE_URL set "BW_READER_PC_OCR_BASE_URL=https://bwicarus.taile44d0c.ts.net"
if not defined BW_READER_PC_OCR_PYTHON set "BW_READER_PC_OCR_PYTHON=%LOCALAPPDATA%\BWReader\reader-pc-ocr-venv\Scripts\python.exe"
if not defined BW_READER_PC_FORMULA_BACKEND set "BW_READER_PC_FORMULA_BACKEND=unimernet-base"
if not defined BW_READER_PC_UNIMERNET_ADAPTER set "BW_READER_PC_UNIMERNET_ADAPTER=reader_unimernet_adapter:create_model"
if not defined BW_READER_PC_UNIMERNET_CONFIG set "BW_READER_PC_UNIMERNET_CONFIG=%BW_PROJECT%\scripts\reader_unimernet_base.yaml"
if not defined BW_READER_PC_UNIMERNET_MODEL_DIR set "BW_READER_PC_UNIMERNET_MODEL_DIR=%LOCALAPPDATA%\BWReader\models\unimernet_base"
if not defined BW_READER_PC_DOCLAYOUT_MODEL set "BW_READER_PC_DOCLAYOUT_MODEL=%LOCALAPPDATA%\BWReader\models\doclayout_yolo\doclayout_yolo_docstructbench_imgsz1024.pt"
if not defined HF_HOME set "HF_HOME=%LOCALAPPDATA%\BWReader\models\hf-cache"
if not defined XDG_CACHE_HOME set "XDG_CACHE_HOME=%LOCALAPPDATA%\BWReader\models\cache"
if not exist "%BW_READER_PC_OCR_PYTHON%" (
  echo PC OCR Python not found. Create the Python 3.10 environment under LocalAppData or set BW_READER_PC_OCR_PYTHON. 1>&2
  exit /b 2
)
set "PYTHONUTF8=1"
"%BW_READER_PC_OCR_PYTHON%" "%BW_PROJECT%\scripts\reader_pc_preprocess_worker.py" --project-root "%BW_PROJECT%" %*
exit /b %ERRORLEVEL%
