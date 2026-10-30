@echo off
rem ============================================
rem  Pet brain server (local HTTP for the QQ bridge)
rem
rem  Keep this file PURE ASCII. cmd.exe decodes .bat with the OEM codepage
rem  (GBK on zh-CN Windows), so UTF-8 Chinese comments turn into garbage
rem  commands and the launch fails at the first line.
rem
rem  Token comes from brain_token.txt next to this file (git-ignored).
rem  If that file is missing, the server generates a random token and logs it:
rem  copy that value into brain_token.txt AND into the AstrBot plugin settings.
rem ============================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found. Create it first:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
if exist "brain_token.txt" (
    set /p PET_BRAIN_TOKEN=<brain_token.txt
) else (
    echo [WARN] brain_token.txt not found - a random token will be used, see the log.
)
.venv\Scripts\python.exe pet\brain_server.py
pause
