@echo off
rem MarginMate: replaces the data of the DEVELOPMENT folder with a copy of a
rem production backup (DEPLOY.md, section 10).
rem
rem   refresh_dev_data.cmd                  the newest of C:\MarginMate\backups
rem   refresh_dev_data.cmd "C:\MarginMate\backups\2026-10-01_101500"
rem
rem Started with a double-click in the development folder. Refuses to run in the
rem production copy (a .env saying MARGINMATE_HTTPS=1) and while the development
rem server runs (something listens on port 8000). The development data folder is
rem the one this folder's .env names (MARGINMATE_TENANTS_ROOT), read by Python
rem with the settings. It is never deleted: it is renamed name.ancien-date, then
rem the backup's data folder is copied in its place.
rem The backup's .env is never copied: this folder keeps its own.
rem No accent in this file: cmd.exe reads it in the console's code page.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "MM_CODE=1"
set "MM_PYTHON=.venv\Scripts\python.exe"
set "MM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "MM_BACKUPS=C:\MarginMate\backups"
set "MM_ANSWERS=%TEMP%\marginmate-refresh-answers-%RANDOM%%RANDOM%.txt"
set "MM_DATA="
set "MM_BACKUP="
set "MM_PREVIOUS="
for /f "tokens=2 delims=:." %%c in ('chcp') do set "MM_CODE_PAGE=%%c"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
title MarginMate - copie des donnees de developpement
echo MarginMate - copie des donnees de production dans le dossier de developpement
echo(

if not exist "%MM_PYTHON%" goto :no_venv

rem 1. The development server holds the databases open: it must be stopped.
call :port_free 8000
if errorlevel 1 goto :dev_server_running

rem 2. The backup: the one given, else the newest (by its name, date and time)
rem that is FINISHED. A backup cut short (window closed, power cut) keeps its
rem name without -INCOMPLET but has no manifest.json, which backup_data writes
rem last: it is passed over, and the one before it taken.
set "MM_SOURCE=%~1"
if defined MM_SOURCE goto :source_chosen
for /f "delims=" %%b in ('dir /b /ad /o-n "%MM_BACKUPS%" 2^>nul ^| findstr /r /x "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]"') do if not defined MM_SOURCE if exist "%MM_BACKUPS%\%%b\manifest.json" if exist "%MM_BACKUPS%\%%b\data\" set "MM_SOURCE=%MM_BACKUPS%\%%b"
if not defined MM_SOURCE goto :no_backup
:source_chosen
rem A folder typed with a trailing backslash would become an escaped quote for Python.
if "%MM_SOURCE:~-1%"=="\" set "MM_SOURCE=%MM_SOURCE:~0,-1%"

rem 3. What this folder's settings say, and what they refuse. Each answer
rem NAME=value becomes MM_NAME: MM_DATA (the development data folder), MM_BACKUP
rem (the backup) and, when there is one, MM_PREVIOUS (the name the current data
rem folder is moved to).
"%MM_PYTHON%" -c "import sys; from accounts import deployment; sys.exit(deployment.main())" development "%MM_SOURCE%" > "%MM_ANSWERS%"
if errorlevel 1 goto :refused
for /f "usebackq tokens=1,* delims==" %%a in ("%MM_ANSWERS%") do set "MM_%%a=%%b"
del "%MM_ANSWERS%" >nul 2>&1

echo Sauvegarde recopiee   : "%MM_BACKUP%"
echo Donnees de developpement : "%MM_DATA%"
if defined MM_PREVIOUS echo Les donnees actuelles seront renommees : "%MM_PREVIOUS%"
echo(
choice /C ON /N /M "Remplacer les donnees de developpement par cette sauvegarde ? (O/N) "
if not "%ERRORLEVEL%"=="1" goto :cancelled

rem 4. The old folder is set aside, never deleted.
if not defined MM_PREVIOUS goto :copy_data
move "%MM_DATA%" "%MM_PREVIOUS%" >nul
if errorlevel 1 goto :rename_failed

:copy_data
rem 5. The copy: the backup's data folder, and it alone (not its .env).
echo Copie en cours...
robocopy "%MM_BACKUP%\data" "%MM_DATA%" /E /R:1 /W:1 /NP /NFL /NDL /NJH
if errorlevel 8 goto :copy_failed

echo(
echo Fait :
if defined MM_PREVIOUS echo - les anciennes donnees de developpement sont dans "%MM_PREVIOUS%" : rien n'a ete efface, supprimez ce dossier vous-meme quand il ne sert plus ;
echo - "%MM_DATA%" est maintenant une copie de "%MM_BACKUP%\data" ;
echo - le .env de la sauvegarde n'a pas ete recopie : ce dossier garde le sien.
echo(
echo ATTENTION : ce sont les VRAIES donnees du bar (factures, banque, personnel), copiees.
echo Elles ne vont jamais dans git, dans un test, une fixture ni un exemple.
echo Si le code de developpement a de nouvelles migrations, lancez ensuite :
echo   .venv\Scripts\python.exe manage.py migrate_tenants
set "MM_CODE=0"
goto :finish

rem ---------------------------------------------------------------------------
rem Refusals and failures.

:no_venv
echo REFUS : le dossier .venv est introuvable a cote de ce fichier. Rien n'a ete fait.
goto :finish

:dev_server_running
echo REFUS : quelque chose ecoute sur le port 8000 : le serveur de developpement
echo (runserver) ou l'apercu tourne. Arretez-le (Ctrl+C dans sa fenetre), puis relancez
echo ce fichier. Rien n'a ete fait.
goto :finish

:no_backup
echo REFUS : aucune sauvegarde terminee dans "%MM_BACKUPS%". Faites-en une dans la
echo copie de production (manage.py backup_data, DEPLOY.md section 8), ou donnez le
echo dossier d'une sauvegarde en argument. Rien n'a ete fait.
goto :finish

:refused
echo(
echo REFUS (la raison est ci-dessus). Rien n'a ete fait.
goto :finish

:cancelled
echo Annule : rien n'a ete fait.
goto :finish

:rename_failed
echo ECHEC : "%MM_DATA%" n'a pas pu etre renomme. Un programme y tient un fichier
echo ouvert (un serveur, un explorateur de base, une fenetre de l'Explorateur ?) :
echo fermez-le, puis relancez ce fichier. Rien n'a ete copie ni efface.
goto :finish

:copy_failed
echo(
echo ECHEC de la copie (lignes ci-dessus) : "%MM_DATA%" est incomplet.
if defined MM_PREVIOUS echo Les donnees d'avant sont intactes dans "%MM_PREVIOUS%".
echo Relancez ce fichier une fois le probleme regle : il mettra ce dossier incomplet
echo de cote a son tour, sans rien effacer.
goto :finish

:finish
del "%MM_ANSWERS%" >nul 2>&1
echo(
pause
if defined MM_CODE_PAGE chcp %MM_CODE_PAGE% >nul
exit /b %MM_CODE%

rem ---------------------------------------------------------------------------
rem Subroutine (call). Argument: the port. Returns 0 when nothing listens there,
rem 1 when something does, 2 when it cannot be read.

:port_free
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "try { $c = @(Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue) } catch { exit 2 }; if ($c.Count -gt 0) { exit 1 } else { exit 0 }"
exit /b %ERRORLEVEL%
