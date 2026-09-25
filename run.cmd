@echo off
rem
rem Launch every-camera on Windows. The counterpart of run.sh, and it is used
rem the same way:
rem
rem   run.cmd --type japan
rem   run.cmd --type japan --config C:\ProgramData\every-camera\japan.json
rem   run.cmd --gui
rem   run.cmd --sentinel            rem the alert watchdog instead of a camera
rem
rem Everything after the script name is passed straight to main.py, so this is a
rem drop-in replacement for `python main.py ...` and adds nothing to learn.
rem
rem What it does for you:
rem   * runs from the program's own directory, so relative paths behave;
rem   * picks the interpreter: %PYTHON%, else a venv beside the checkout, else
rem     python from PATH;
rem   * calls env.cmd if it exists, which is where machine-specific settings
rem     belong (DCAM_LIB, a conda activation, a proxy). That file is deliberately
rem     untracked: it describes this machine, not the program.
rem
rem One thing run.sh does that this cannot: `exec`. cmd.exe has no way to replace
rem itself with another process, so this wrapper stays in the middle for the life
rem of the run. It matters for stopping — see the comment above the last line.
setlocal EnableDelayedExpansion

set "APP_DIR=%~dp0"
rem Strip the trailing backslash %~dp0 always carries; paths are joined below.
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
cd /d "%APP_DIR%"

rem Machine-local environment, if the operator wrote one. Copy env.cmd.example
rem to env.cmd to get started; it is in .gitignore, so a station's paths never
rem end up in a commit.
if exist "%APP_DIR%\env.cmd" call "%APP_DIR%\env.cmd"

rem %PYTHON% wins, then a venv next to the checkout, then whatever is on PATH.
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

rem Which program to run. main.py unless the first argument names the other
rem entry point that has to survive on this machine's terms.
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

rem No exec here, so this wrapper is the process the console sends its events to
rem and Python is its child. Ctrl+C and Ctrl+Break reach both, which is what we
rem want — the driver handles them and shoots its closing darks. Closing the
rem console *window* is the case to know about: Windows gives the whole tree a
rem few seconds and then ends it, so a long closing dark run can still be cut
rem short. Stop a camera with Ctrl+C, not with the X button.
"%PYTHON%" "%TARGET%" %ARGS%
exit /b %ERRORLEVEL%

:nopython
echo run.cmd: no python found. Set PYTHON=C:\path\to\python.exe, or put one in >&2
echo          env.cmd, or create a venv at %APP_DIR%\venv >&2
exit /b 1
