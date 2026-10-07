@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Merger
set "PORT=5000"
set "URL=http://localhost:%PORT%"

rem --- already running? then just open it
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing '%URL%/login' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  start "" "%URL%"
  exit /b 0
)

rem --- find Python
set "PY="
where py >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython
%PY% --version >nul 2>&1
if errorlevel 1 goto :nopython

rem --- first run: a private environment and the libraries
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Merger for the first time. This takes a minute.
  %PY% -m venv .venv
  if errorlevel 1 goto :setupfail
)
set "VPY=%~dp0.venv\Scripts\python.exe"
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
  echo Installing libraries...
  "%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto :setupfail
  copy /y requirements.txt .venv\requirements.installed >nul
)

if not exist "client_secret.json" (
  echo.
  echo client_secret.json was not found in this folder.
  echo Download it from Google Cloud, save it next to Merger.bat, then run this again.
  echo The steps are in README.md.
  echo.
  pause
  exit /b 1
)

rem --- open the browser as soon as the server answers
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i=0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing '%URL%/login' -TimeoutSec 1 | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Milliseconds 500 } }"

echo Merger is starting at %URL%
echo Keep this window open while you use Merger. Close it to stop.
echo.
"%VPY%" app.py
echo.
echo Merger has stopped.
pause
exit /b 0

:nopython
echo Python 3.9 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:setupfail
echo.
echo Setup did not finish. Check your internet connection and try again.
pause
exit /b 1
