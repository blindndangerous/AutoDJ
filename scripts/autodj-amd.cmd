@echo off
rem Compatibility entry point; all hardware now uses the common launcher.
call "%~dp0..\autodj.cmd" %*
exit /b %ERRORLEVEL%
