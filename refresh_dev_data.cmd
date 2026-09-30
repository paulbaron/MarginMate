@echo off
rem MarginMate : remplace les donnees du dossier de DEVELOPPEMENT par une copie d'une
rem sauvegarde de la production (DEPLOY.md, section 10).
rem
rem   refresh_dev_data.cmd                  la plus recente de C:\MarginMate\backups
rem   refresh_dev_data.cmd "C:\MarginMate\backups\2026-10-01_101500"
rem
rem Se lance d'un double-clic dans le dossier de developpement. Refuse de tourner dans
rem la copie de production (un .env qui dit MARGINMATE_HTTPS=1) et tant que le serveur
rem de developpement tourne (quelque chose ecoute sur le port 8000). Le dossier des
rem donnees de developpement est celui du .env de ce dossier (MARGINMATE_TENANTS_ROOT),
rem lu par Python avec les reglages. Il n'est jamais efface : il est renomme
rem nom.ancien-date, puis le dossier data de la sauvegarde est recopie a sa place.
rem Le .env de la sauvegarde n'est jamais recopie : ce dossier garde le sien.
rem Pas d'accent dans ce fichier : cmd.exe le lit dans la page de code de la console.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "MM_CODE=1"
set "MM_PYTHON=.venv\Scripts\python.exe"
set "MM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "MM_SAUVEGARDES=C:\MarginMate\backups"
set "MM_INFOS=%TEMP%\marginmate-refresh-infos-%RANDOM%%RANDOM%.txt"
for /f "tokens=2 delims=:." %%c in ('chcp') do set "MM_PAGE_DE_CODE=%%c"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
title MarginMate - copie des donnees de developpement
echo MarginMate - copie des donnees de production dans le dossier de developpement
echo(

if not exist "%MM_PYTHON%" goto :pas_de_venv

rem 1. Le serveur de developpement tient les bases ouvertes : il doit etre arrete.
call :port_libre 8000
if errorlevel 1 goto :serveur_dev_lance

rem 2. La sauvegarde : celle donnee, sinon la plus recente (par son nom, date et heure)
rem qui est TERMINEE. Une sauvegarde coupee (fenetre fermee, panne de courant) garde son
rem nom sans -INCOMPLET mais n'a pas de manifest.json, que backup_data ecrit en dernier :
rem elle est passee, la precedente est prise.
set "MM_SOURCE=%~1"
if defined MM_SOURCE goto :source_choisie
for /f "delims=" %%b in ('dir /b /ad /o-n "%MM_SAUVEGARDES%" 2^>nul ^| findstr /r /x "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]"') do if not defined MM_SOURCE if exist "%MM_SAUVEGARDES%\%%b\manifest.json" if exist "%MM_SAUVEGARDES%\%%b\data\" set "MM_SOURCE=%MM_SAUVEGARDES%\%%b"
if not defined MM_SOURCE goto :aucune_sauvegarde
:source_choisie
rem Un dossier tape avec une barre oblique finale deviendrait un guillemet echappe pour Python.
if "%MM_SOURCE:~-1%"=="\" set "MM_SOURCE=%MM_SOURCE:~0,-1%"

rem 3. Ce que disent les reglages de ce dossier, et ce qu'ils refusent.
"%MM_PYTHON%" -c "import sys; from accounts import deployment; sys.exit(deployment.main())" developpement "%MM_SOURCE%" > "%MM_INFOS%"
if errorlevel 1 goto :refus
for /f "usebackq tokens=1,* delims==" %%a in ("%MM_INFOS%") do set "MM_%%a=%%b"
del "%MM_INFOS%" >nul 2>&1

echo Sauvegarde recopiee   : "%MM_SAUVEGARDE%"
echo Donnees de developpement : "%MM_DONNEES%"
if defined MM_ANCIEN echo Les donnees actuelles seront renommees : "%MM_ANCIEN%"
echo(
choice /C ON /N /M "Remplacer les donnees de developpement par cette sauvegarde ? (O/N) "
if not "%ERRORLEVEL%"=="1" goto :annule

rem 4. L'ancien dossier est mis de cote, jamais efface.
if not defined MM_ANCIEN goto :copier
move "%MM_DONNEES%" "%MM_ANCIEN%" >nul
if errorlevel 1 goto :renommage_impossible

:copier
rem 5. La copie : le dossier data de la sauvegarde, et lui seul (pas son .env).
echo Copie en cours...
robocopy "%MM_SAUVEGARDE%\data" "%MM_DONNEES%" /E /R:1 /W:1 /NP /NFL /NDL /NJH
if errorlevel 8 goto :copie_echouee

echo(
echo Fait :
if defined MM_ANCIEN echo - les anciennes donnees de developpement sont dans "%MM_ANCIEN%" : rien n'a ete efface, supprimez ce dossier vous-meme quand il ne sert plus ;
echo - "%MM_DONNEES%" est maintenant une copie de "%MM_SAUVEGARDE%\data" ;
echo - le .env de la sauvegarde n'a pas ete recopie : ce dossier garde le sien.
echo(
echo ATTENTION : ce sont les VRAIES donnees du bar (factures, banque, personnel), copiees.
echo Elles ne vont jamais dans git, dans un test, une fixture ni un exemple.
echo Si le code de developpement a de nouvelles migrations, lancez ensuite :
echo   .venv\Scripts\python.exe manage.py migrate_tenants
set "MM_CODE=0"
goto :fin

rem ---------------------------------------------------------------------------
rem Refus et echecs.

:pas_de_venv
echo REFUS : le dossier .venv est introuvable a cote de ce fichier. Rien n'a ete fait.
goto :fin

:serveur_dev_lance
echo REFUS : quelque chose ecoute sur le port 8000 : le serveur de developpement
echo (runserver) ou l'apercu tourne. Arretez-le (Ctrl+C dans sa fenetre), puis relancez
echo ce fichier. Rien n'a ete fait.
goto :fin

:aucune_sauvegarde
echo REFUS : aucune sauvegarde terminee dans "%MM_SAUVEGARDES%". Faites-en une dans la
echo copie de production (manage.py backup_data, DEPLOY.md section 8), ou donnez le
echo dossier d'une sauvegarde en argument. Rien n'a ete fait.
goto :fin

:refus
echo(
echo REFUS (la raison est ci-dessus). Rien n'a ete fait.
goto :fin

:annule
echo Annule : rien n'a ete fait.
goto :fin

:renommage_impossible
echo ECHEC : "%MM_DONNEES%" n'a pas pu etre renomme. Un programme y tient un fichier
echo ouvert (un serveur, un explorateur de base, une fenetre de l'Explorateur ?) :
echo fermez-le, puis relancez ce fichier. Rien n'a ete copie ni efface.
goto :fin

:copie_echouee
echo(
echo ECHEC de la copie (lignes ci-dessus) : "%MM_DONNEES%" est incomplet.
if defined MM_ANCIEN echo Les donnees d'avant sont intactes dans "%MM_ANCIEN%".
echo Relancez ce fichier une fois le probleme regle : il mettra ce dossier incomplet
echo de cote a son tour, sans rien effacer.
goto :fin

:fin
del "%MM_INFOS%" >nul 2>&1
echo(
pause
if defined MM_PAGE_DE_CODE chcp %MM_PAGE_DE_CODE% >nul
exit /b %MM_CODE%

rem ---------------------------------------------------------------------------
rem Sous-programme (call). Argument : le port. Rend 0 quand rien n'y ecoute, 1 quand
rem quelque chose y ecoute, 2 quand il ne se lit pas.

:port_libre
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "try { $c = @(Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue) } catch { exit 2 }; if ($c.Count -gt 0) { exit 1 } else { exit 0 }"
exit /b %ERRORLEVEL%
