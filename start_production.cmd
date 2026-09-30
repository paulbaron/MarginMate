@echo off
rem MarginMate: the production server (see DEPLOY.md).
rem
rem Runs "manage.py serve" with the Python of the .venv folder: that is the
rem virtual environment itself, no need to activate it, nor uv to serve.
rem "serve" checks everything before it starts (debug mode off, secret key,
rem host names, HTTPS, migrations), then serves the site on
rem http://127.0.0.1:8765, the address the Cloudflare tunnel points at (not
rem 8000: that is the port of runserver, the development server).
rem Ctrl+C to stop it. Options are passed on: start_production.cmd --port 8002
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Le dossier .venv est introuvable a cote de ce fichier : installez d'abord MarginMate, voir README.md.
    pause
    exit /b 1
)
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" manage.py serve %*
set CODE=%ERRORLEVEL%
if not "%CODE%"=="0" (
    echo.
    echo Le serveur ne tourne pas : lisez les lignes ci-dessus, corrigez, puis relancez ce fichier.
    pause
)
exit /b %CODE%
