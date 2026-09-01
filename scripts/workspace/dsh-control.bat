@echo off
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\..\..\.."
set "ROOT=%CD%"
popd
set "REPO=%ROOT%\my-deepseek-harness\deepseek-harness"
set "PORT=3080"

if /i "%~1"=="start"   goto start
if /i "%~1"=="stop"    goto stop
if /i "%~1"=="restart" goto restart
if /i "%~1"=="status"  goto status
echo Usage: %~nx0 start or stop or restart or status
exit /b 2

:start
set "NODE_OPTIONS="
set "DSH_PERMISSION_MODE=danger-full-access"
set "DSH_HOME=%ROOT%\.dsh_home"
set "DEEPSEEK_API_KEY="
rem Parse DEEPSEEK_API_KEY from the repo .env (non-standard "deepseek API key = sk-..." line)
for /f "usebackq tokens=* delims=" %%l in (`findstr /i "deepseek" "%REPO%\.env" 2^>nul`) do (
  if not defined DEEPSEEK_API_KEY (
    for /f "tokens=2 delims==" %%k in ("%%l") do (
      set "DEEPSEEK_API_KEY=%%k"
    )
  )
)
rem Trim leading spaces from the key
if defined DEEPSEEK_API_KEY (
  for /f "tokens=*" %%t in ("%DEEPSEEK_API_KEY%") do set "DEEPSEEK_API_KEY=%%t"
)
if not exist "%ROOT%\outputs" mkdir "%ROOT%\outputs"
if not exist "%DSH_HOME%" mkdir "%DSH_HOME%"
set "LOG=%ROOT%\outputs\dsh_web.log"
cd /d "%REPO%"
echo [START] Launching dsh web profile (port %PORT%) - logs at %LOG%
if not defined DEEPSEEK_API_KEY (
  echo [WARN] DEEPSEEK_API_KEY not found in .env - LLM calls will fail
)
rem Fetch provider enabled via the committed overlay (scripts\workspace\web-fetch-overlay.yml).
rem base bundle intentionally leaves fetch off for SSRF safety; the overlay is the
rem supported per-deployment enablement, so wire it into the default launch.
node --expose-internals apps\cli\lib\bin.js --profile web --patch scripts\workspace\web-fetch-overlay.yml >> "%LOG%" 2>&1
echo [STOP] dsh web exited
exit /b 0

:stop
netstat -ano 2>nul | findstr ":%PORT%" | findstr "LISTENING" > "%ROOT%\outputs\pid.txt"
for /f "tokens=5" %%p in (%ROOT%\outputs\pid.txt) do taskkill /PID %%p /T /F >nul 2>&1
echo [OK] Stop signal sent
exit /b 0

:restart
call "%~f0" stop
call "%~f0" start
exit /b 0

:status
netstat -ano 2>nul | findstr ":%PORT%" | findstr "LISTENING" >nul
if errorlevel 1 ( echo [STATUS] dsh web stopped ) else ( echo [STATUS] dsh web running on port %PORT% )
exit /b 0
