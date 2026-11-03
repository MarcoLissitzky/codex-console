@echo off
rem Open the console as a standalone window (no address bar, no tabs). Port comes from settings.json.
cd /d "%~dp0"
set PORT=8770
for /f %%p in ('python console.py --print-port 2^>nul') do set PORT=%%p
powershell -NoProfile -Command "if (-not (Test-NetConnection 127.0.0.1 -Port %PORT% -InformationLevel Quiet -WarningAction SilentlyContinue)) { Start-Process -FilePath 'pythonw.exe' -ArgumentList 'console.py' -WorkingDirectory '%CD%' -WindowStyle Hidden; Start-Sleep -Seconds 3 }"
start "" msedge --app=http://127.0.0.1:%PORT% --window-size=1440,960
