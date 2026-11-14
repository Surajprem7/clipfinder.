@echo off
rem clipfinder: one-time setup on Windows (for the source folder, not for ClipFinder-Setup.exe).
rem Installs Python, ffmpeg (with winget, which comes with Windows 10/11) and the Python parts numpy + yt-dlp.
rem Safe to run again: anything already installed is skipped.
setlocal
where winget >nul 2>nul
if errorlevel 1 (
  echo winget was not found. Install "App Installer" from the Microsoft Store, or use ClipFinder-Setup.exe instead.
  pause
  exit /b 1
)
echo.
echo [1/3] Python
rem Find a Python that really runs. "where python" is not enough: Windows has a "python" shortcut that only
rem opens the Microsoft Store, so each candidate is started once to prove it works.
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY="%LocalAppData%\Programs\Python\Python312\python.exe""
if not defined PY if exist "%LocalAppData%\Programs\Python\Launcher\py.exe" set "PY="%LocalAppData%\Programs\Python\Launcher\py.exe" -3"
if not defined PY (
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  set "PY="
  py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
  if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
  if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY="%LocalAppData%\Programs\Python\Python312\python.exe""
  if not defined PY if exist "%LocalAppData%\Programs\Python\Launcher\py.exe" set "PY="%LocalAppData%\Programs\Python\Launcher\py.exe" -3"
)
if not defined PY (
  echo.
  echo Python could not be installed or found. Install Python 3.12 from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH"^), then run this setup again.
  pause
  exit /b 1
)
echo Python: %PY%
echo.
echo [2/3] ffmpeg
where ffmpeg >nul 2>nul || winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
echo.
echo [3/3] numpy and yt-dlp
%PY% -m pip install --upgrade numpy yt-dlp
if errorlevel 1 (
  echo.
  echo Installing numpy / yt-dlp failed. Check the internet connection and run this setup again.
  pause
  exit /b 1
)
echo.
echo Done. CLOSE this window, then double-click start_clipfinder.bat
echo (a new window sees the newly installed programs).
pause
