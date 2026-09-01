@echo off
REM ============================================================
REM  clean-workspace.bat
REM  Remove transient and regenerable artifacts from the workspace.
REM  Keeps vintage macro data (macro_indicators.sqlite) and the
REM  fork repository (my-deepseek-harness) untouched.
REM ============================================================
setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%..\..\..\.."
set "ROOT=%CD%"
popd

echo [1/3] Remove temporary .dsh_home run dirs ...
if exist "%ROOT%\.dsh_home" rmdir /s /q "%ROOT%\.dsh_home"

echo [2/3] Remove web runtime log ...
if exist "%ROOT%\outputs\dsh_web.log" del /q "%ROOT%\outputs\dsh_web.log"

echo [3/3] Remove regenerable reports ...
for /r "%ROOT%\outputs" %%f in (macro_collection_report.md) do del /q "%%f" 2>nul

echo.
echo Done. Vintage data and fork repo kept.
pause
