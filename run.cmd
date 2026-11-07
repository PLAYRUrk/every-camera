@echo off
rem Arguments go straight to main.py; --sentinel starts the watchdog instead.
setlocal EnableDelayedExpansion

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
cd /d "%APP_DIR%"

if exist "%APP_DIR%\env.cmd" call "%APP_DIR%\env.cmd"

if not defined PYTHON (
    if exist "%APP_DIR%\venv\Scripts\python.exe" (
        set "PYTHON=%APP_DIR%\venv\Scripts\python.exe"
    ) else if exist "%APP_DIR%\.venv\Scripts\python.exe" (
        set "PYTHON=%APP_DIR%\.venv\Scripts\python.exe"
    )
)
if not defined PYTHON (
    for %%P in (python.exe) do set "PYTHON=%%~$PATH:P"
)

if not defined PYTHON goto :nopython
if not exist "%PYTHON%" goto :nopython

set "TARGET=%APP_DIR%\main.py"
set "ARGS=%*"
if /I "%~1"=="--sentinel" (
    set "TARGET=%APP_DIR%\sentinel.py"
    shift
    set "ARGS="
    :shift_loop
    if not "%~1"=="" (
        set "ARGS=!ARGS! %1"
        shift
        goto :shift_loop
    )
)

rem cmd.exe has no exec, so this wrapper stays as the parent. Ctrl+C and
rem Ctrl+Break reach both processes, but closing the window gives the tree only
rem a few seconds, which can cut a long closing dark run short.
"%PYTHON%" "%TARGET%" %ARGS%
exit /b %ERRORLEVEL%

:nopython
echo run.cmd: no python found. Set PYTHON=C:\path\to\python.exe, or put one in >&2
echo          env.cmd, or create a venv at %APP_DIR%\venv >&2
exit /b 1
