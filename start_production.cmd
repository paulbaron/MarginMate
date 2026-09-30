@echo off
rem MarginMate : le serveur de production (voir DEPLOY.md).
rem
rem Lance "manage.py serve" avec le Python du dossier .venv : c'est
rem l'environnement virtuel lui-meme, pas besoin de l'activer. "serve"
rem verifie tout avant de demarrer (mode debug coupe, cle secrete, noms
rem d'hote, HTTPS, migrations), puis sert le site sur http://127.0.0.1:8765,
rem l'adresse vers laquelle pointe le tunnel Cloudflare (pas 8000 : c'est
rem le port de runserver, le serveur de developpement).
rem Ctrl+C pour l'arreter. Les options sont transmises : start_production.cmd --port 8002
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
