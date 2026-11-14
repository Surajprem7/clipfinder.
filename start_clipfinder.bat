@echo off
rem clipfinder: double-click to start. Videos, analyses and clips go to the 'data' folder next to this program's folder.
rem To use another folder, change the DATA line below.
setlocal
set "DATA=%~dp0..\data"
cd /d "%~dp0"
rem Find a Python that really runs. "where python" is not enough: Windows has a "python" shortcut that only
rem opens the Microsoft Store, so each candidate is started once to prove it works.
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
if not defined PY if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY="%LocalAppData%\Programs\Python\Python312\python.exe""
if not defined PY if exist "%LocalAppData%\Programs\Python\Launcher\py.exe" set "PY="%LocalAppData%\Programs\Python\Launcher\py.exe" -3"
if not defined PY (
  echo Python was not found. Run setup_windows.bat first, close its window, then start this again.
  pause
  exit /b 1
)
%PY% clipfinder_web.py --data "%DATA%"
echo.
echo clipfinder stopped. If it did not start: check that Python and ffmpeg are installed (see README.md).
pause
