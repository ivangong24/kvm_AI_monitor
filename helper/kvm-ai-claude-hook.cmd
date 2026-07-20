@echo off
rem Claude Code lifecycle hook (Windows): forwards start/active/stop to the push helper in the
rem background. Must never slow down or break Claude Code, so it never waits and always exits 0.
set EVENT=%1
if "%EVENT%"=="" set EVENT=active
start /b "" pythonw "%~dp0kvm_ai_push.py" send-activity %EVENT% >nul 2>&1
exit /b 0
