@echo off
rem clipfinder: double-click to start. Videos, analyses and clips go to the 'data' folder next to this program's folder.
rem To use another folder, change the DATA line below.
set "DATA=%~dp0..\data"
cd /d "%~dp0"
where python >nul 2>nul && (set "PY=python") || (set "PY=py")
%PY% clipfinder_web.py --data "%DATA%"
echo.
echo clipfinder stopped. If it did not start: check that Python and ffmpeg are installed (see README.md).
pause
