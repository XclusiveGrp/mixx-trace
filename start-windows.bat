@echo off
rem Mixx Trace launcher for Windows. Double-click it.
cd /d "%~dp0"
set SD_ENABLE_ASIO=1

set PY=py -3
where py >nul 2>nul
if errorlevel 1 set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found.
  echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and run this file again.
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo First run: setting up Mixx Trace, about a minute, needs internet once...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
  .venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo Installing the requirements failed. Check the internet connection and run this file again.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

echo Starting Mixx Trace. Leave this window open while you work; close it to quit.
.venv\Scripts\python.exe mixxtrace.py %*
if errorlevel 1 pause
