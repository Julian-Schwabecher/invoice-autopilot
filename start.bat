@echo off
rem Double-click launcher for Windows.
cd /d "%~dp0"

where uv >/dev/null 2>nul
if errorlevel 1 goto nouv
goto run

:nouv
echo Invoice Autopilot needs a small helper called uv.
echo It is free and safe, and it only has to be installed once.
echo.
set "ANSWER="
set /p "ANSWER=Install it now? Type y and press Enter: "
if /i not "%ANSWER%"=="y" goto fail
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 goto fail
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >/dev/null 2>nul
if errorlevel 1 goto installed
goto run

:installed
echo.
echo Installed. Please close this window, then double-click start.bat again.
pause
exit /b 0

:run
uv run python run.py
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo Something went wrong. The message above says why.
pause
exit /b 1
