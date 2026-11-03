@echo off
rem Codex auto-resume watchdog launcher (PowerShell scripts are blocked by policy on this machine).
rem Usage: watch.cmd [--session ID] [--check] [--max-resumes N] [--max-wait SECONDS]
cd /d "%~dp0"
python codex_auto_resume.py %*
