@echo off
setlocal
pushd "%~dp0.." >nul
if errorlevel 1 exit /b 1

set "AUTODJ_GPU_ENV=%AUTODJ_AMD_ENV%"
if not defined AUTODJ_GPU_ENV set "AUTODJ_GPU_ENV=.uv\amd"
for %%I in ("%AUTODJ_GPU_ENV%") do set "AUTODJ_GPU_ENV=%%~fI"

if not exist "%AUTODJ_GPU_ENV%\Scripts\autodj.exe" (
  echo AMD AutoDJ environment not found. Follow docs/windows-amd.md to set it up.
  popd
  exit /b 2
)

if not exist "%AUTODJ_GPU_ENV%\miopen\db" mkdir "%AUTODJ_GPU_ENV%\miopen\db" >nul 2>&1
if not exist "%AUTODJ_GPU_ENV%\miopen\cache" mkdir "%AUTODJ_GPU_ENV%\miopen\cache" >nul 2>&1
set "MIOPEN_USER_DB_PATH=%AUTODJ_GPU_ENV%\miopen\db"
set "MIOPEN_CUSTOM_CACHE_DIR=%AUTODJ_GPU_ENV%\miopen\cache"

"%AUTODJ_GPU_ENV%\Scripts\autodj.exe" %*
set "autodjExitCode=%ERRORLEVEL%"
popd
exit /b %autodjExitCode%
