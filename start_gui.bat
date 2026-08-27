@echo off
setlocal
cd /d "%~dp0"
title council GUI launcher

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH. Please install Python 3.10+ first.
  pause
  exit /b 1
)

python -c "import yaml, customtkinter" >nul 2>nul
if errorlevel 1 (
  echo [setup] Installing dependencies: pyyaml, customtkinter ...
  python -m pip install pyyaml customtkinter --quiet --disable-pip-version-check
  if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check network or pip config.
    pause
    exit /b 1
  )
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw gui.py
) else (
  start "" python gui.py
)
exit /b 0
