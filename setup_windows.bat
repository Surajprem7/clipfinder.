@echo off
rem clipfinder: one-time setup on Windows (for the source folder, not for ClipFinder-Setup.exe).
rem Installs Python, ffmpeg (with winget, which comes with Windows 10/11) and the Python parts numpy + yt-dlp.
rem NOT TESTED on a real Windows computer yet - if a line fails, tell me which one and its message.
setlocal
where winget >nul 2>nul
if errorlevel 1 (
  echo winget was not found. Install "App Installer" from the Microsoft Store, or use ClipFinder-Setup.exe instead.
  pause
  exit /b 1
)
echo.
echo [1/3] Python
where python >nul 2>nul || where py >nul 2>nul || winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo [2/3] ffmpeg
where ffmpeg >nul 2>nul || winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
echo.
echo [3/3] numpy and yt-dlp
where python >nul 2>nul && (python -m pip install --upgrade numpy yt-dlp) || (py -m pip install --upgrade numpy yt-dlp)
echo.
echo Done. If a message above says a program was just installed, CLOSE this window and then
echo double-click start_clipfinder.bat (a new window sees the newly installed programs).
pause
