@echo off
rem ==========================================================
rem  PasteMate launcher (double-click to run)
rem  - Single instance: re-launching just wakes the existing one
rem  - First run: auto-installs dependencies via pip
rem  IMPORTANT: keep this file ASCII-only! Non-ASCII characters
rem  break batch parsing on non-UTF8 consoles (GBK etc).
rem ==========================================================
cd /d "%~dp0"

set "PY="
where pythonw >nul 2>nul && set "PY=pythonw"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [PasteMate] Python not found. Please install Python 3.8+ and add it to PATH.
    pause
    exit /b 1
)

%PY% -c "import pynput, pyperclip, pystray; from PIL import Image, ImageDraw" >nul 2>nul
if not %errorlevel%==0 (
    echo [PasteMate] Installing dependencies, please wait...
    %PY% -m pip install -r "%~dp0requirements.txt"
)

start "PasteMate" %PY% "%~dp0pastemate.py"
exit /b 0
