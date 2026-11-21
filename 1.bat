@echo off
REM ============================================================
REM  AV_Wall quick launcher (debug convenience)
REM
REM  NOTE: keep this file pure ASCII.
REM  cmd reads .bat with the OEM code page (936/GBK here). UTF-8
REM  Chinese bytes get mangled and then parsed as commands.
REM  So: no Chinese characters in this file, and this file starts
REM  server.py directly instead of calling the Chinese-named
REM  launcher (that name would be mangled too).
REM
REM  Usage:  1.bat [port]        (default port 8787)
REM ============================================================
setlocal enabledelayedexpansion
REM Switch the console to UTF-8 so server.py's Chinese banner prints
REM correctly instead of mojibake. 65001 works on Windows 10+.
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cls
pushd "%~dp0" 2>nul

REM ---- optional port argument ----
set "PORT=%~1"
if not defined PORT set "PORT=8787"

REM ---- locate python interpreter (priority order) ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY (
  if exist "D:\Program Files\Python310\python.exe" set "PY=D:\Program Files\Python310\python.exe"
)
if not defined PY (
  echo [avwall] Could not find Python. Please install Python 3.10 or add it to PATH.
  pause
  exit /b 1
)

REM ---- kill any old process holding the port, so a restart really takes effect ----
REM findstr /C:":8787 " is an EXACT substring match on the port column.
REM Do NOT use a regex like ":8787 .*LISTENING" here: that is a
REM substring match too and it also matches ports like 7, 9, 80, 135,
REM 445, 3389 ... which would make this script kill unrelated system
REM processes (PID 4 = System, 135 = RPC, 445 = SMB ...).
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
  if not "%%a"=="" (
    echo [avwall] Killing old process on port %PORT%, PID=%%a ...
    taskkill /F /PID %%a >nul 2>nul
  )
)
REM ping -n 2 is used instead of "timeout /t 1": ping works the same
REM in cmd.exe and in Git Bash, while "timeout" may be shadowed by the
REM GNU coreutils timeout that ships with Git Bash.
ping -n 2 127.0.0.1 >nul

REM ---- verify the port is really free before starting ----
REM If the old process could not be killed, server.py would fail to bind.
REM On Windows a second process can hijack an already-listening port,
REM which used to make two servers share the port and answer requests
REM at random.  server.py now refuses to start in that case and prints a
REM clear message, but it is friendlier to say it here.
set "PORT_BUSY=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
  if not "%%a"=="" set "PORT_BUSY=1"
)
if "!PORT_BUSY!"=="1" (
  echo [avwall] Port %PORT% is still in use. The old process could not be stopped.
  echo [avwall] Close the window that runs the old server ^(or press Ctrl+C there^), then try again.
  pause
  exit /b 1
)

"%PY%" "%~dp0server.py" --port %PORT%
endlocal
