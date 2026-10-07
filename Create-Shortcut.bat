@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = [Environment]::GetFolderPath('Desktop'); $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Merger.lnk')); $s.TargetPath = '%~dp0Merger.bat'; $s.WorkingDirectory = '%~dp0'; $s.IconLocation = '%~dp0favicon.ico'; $s.WindowStyle = 7; $s.Description = 'Merger storage pool'; $s.Save()"
if errorlevel 1 (
  echo Could not create the shortcut.
) else (
  echo A Merger shortcut with the new icon is now on your Desktop.
)
pause
