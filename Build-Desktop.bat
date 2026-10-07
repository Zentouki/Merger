@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build the Merger desktop app

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

if not exist ".build-venv\Scripts\python.exe" (
  echo Preparing the build environment. This takes a few minutes.
  %PY% -m venv .build-venv
  if errorlevel 1 goto :fail
)
set "VPY=%~dp0.build-venv\Scripts\python.exe"
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt -r requirements-desktop.txt
if errorlevel 1 goto :fail

echo Building Merger.exe ...
"%VPY%" -m PyInstaller --noconfirm --clean --windowed --name Merger --icon favicon.ico ^
  --add-data "index.html;." --add-data "login.html;." ^
  --collect-data googleapiclient --collect-submodules keyring --hidden-import waitress ^
  desktop.py
if errorlevel 1 goto :fail

echo.
echo Done. Your app is in dist\Merger\Merger.exe
echo To make a setup program, install Inno Setup and compile installer.iss.
echo.
pause
exit /b 0

:nopython
echo Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this again.
pause
exit /b 1

:fail
echo.
echo The build did not finish. Scroll up to see the first error.
pause
exit /b 1
