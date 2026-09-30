@echo off
rem MarginMate: puts the latest version of the code online (DEPLOY.md, section 10).
rem
rem Started with a double-click in the PRODUCTION copy, C:\MarginMate\app: a git
rem clone whose "origin" remote is the development folder. It takes what was
rem committed (git commit) on the main branch there, in this order:
rem   0. one deployment at a time: it creates the folder
rem      .git\marginmate-deploy (mkdir, which fails when it already exists), notes
rem      there how far it got (etat.txt), and removes it only once finished, or after
rem      a failure that put the server back as it was. Left behind (a failure after
rem      the code update, a window closed half way), it makes the next deployment
rem      refuse, which prints what was noted and how to go back;
rem   1. refuses to run anywhere but in production: a .env saying
rem      DJANGO_DEBUG=True, or not MARGINMATE_HTTPS=1, is the development one;
rem   2. git fetch origin, shows the changes, asks for confirmation. Nothing
rem      new: checks that the site listens, and says so when it is offline;
rem   3. refuses while a job runs in a tenant (manage.py running_jobs), and
rem      when the scheduled task MarginMate starts another folder than this one;
rem   4. stops the server: the scheduled task MarginMate, then whatever listens
rem      on 127.0.0.1:8765. A job started between the check and the stop: the
rem      server is restarted as it was, the deployment refused;
rem   5. backs up the data (manage.py backup_data). If that fails: the server is
rem      restarted as it was, nothing has changed;
rem   6. git merge --ff-only origin/main. If that fails: back to the previous
rem      code, the server is restarted;
rem   7. uv sync --locked --no-dev, manage.py migrate_tenants, manage.py serve
rem      --verifier. If a step fails: the server is NOT restarted, the mark of
rem      step 0 stays, and the window says how to go back;
rem   8. restarts the server and waits until it listens on 127.0.0.1:8765.
rem Before step 0 it refuses, touching nothing, a folder without .venv, a folder
rem that is not a git clone, and a PC where git or uv does not answer.
rem
rem This file is part of the code git updates, and cmd.exe reads a .cmd file
rem line by line from disk while it runs: it first copies itself to the
rem temporary folder and goes on from that copy.
rem No accent in this file: cmd.exe reads it in the console's code page.
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--from-the-copy" goto :from_the_copy
set "MM_COPY=%TEMP%\marginmate-deploy-%RANDOM%%RANDOM%.cmd"
copy /y "%~f0" "%MM_COPY%" >nul
if errorlevel 1 goto :cannot_copy
rem Without call: control passes to the copy and never comes back here.
"%MM_COPY%" --from-the-copy "%~dp0"

:cannot_copy
echo Impossible de recopier deploy.cmd dans le dossier temporaire : rien n'a ete fait.
pause
exit /b 1

:from_the_copy
set "MM_APP=%~2"
set "MM_CODE=1"
cd /d "%MM_APP%"
if errorlevel 1 goto :folder_not_found
set "MM_PORT=8765"
set "MM_TASK=MarginMate"
set "MM_PYTHON=.venv\Scripts\python.exe"
set "MM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "MM_ANSWERS=%TEMP%\marginmate-deploy-answers-%RANDOM%%RANDOM%.txt"
set "MM_BACKUP_PATH_FILE=%TEMP%\marginmate-deploy-backup-path-%RANDOM%%RANDOM%.txt"
set "MM_LOCK=%MM_APP%.git\marginmate-deploy"
set "MM_STATE=%MM_APP%.git\marginmate-deploy\etat.txt"
set "MM_RELEASE=0"
set "MM_BRANCH="
set "MM_DATA="
set "MM_BACKUP="
set "MM_PREVIOUS="
set "MM_PREVIOUS_SHORT="
set "MM_NEW_SHORT="
set "MM_WAS_RUNNING=0"
set "MM_STEP=la preparation"
for /f "tokens=2 delims=:." %%c in ('chcp') do set "MM_CODE_PAGE=%%c"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
title MarginMate - mise en ligne
echo MarginMate - mise en ligne d'une nouvelle version
echo Dossier : "%MM_APP%"
echo(

if not exist "%MM_PYTHON%" goto :no_venv
if not exist ".git\" goto :not_a_clone
where git >nul 2>&1
if errorlevel 1 goto :no_git
rem uv installs the dependencies (step 7). Asked by running it: a mise shim on
rem the PATH is there even when mise is gone. With call, as git: a shim may be
rem a batch file, which would not hand control back without it.
call uv --version >nul
if errorlevel 1 goto :no_uv

rem 0. One deployment at a time. mkdir is atomic: of two windows, only one
rem creates the folder. After the clone check: mkdir would create a missing
rem .git. Never a file held open (9 and a redirection): cmd would hand it to the
rem server's window that start opens, which would keep it for its whole life.
mkdir "%MM_LOCK%" 2>nul || goto :already_running
set "MM_RELEASE=1"

rem 1. The production copy, and nothing else. Each answer NAME=value becomes
rem MM_NAME: MM_DATA, the data folder.
"%MM_PYTHON%" -c "import sys; from accounts import deployment; sys.exit(deployment.main())" production > "%MM_ANSWERS%"
if errorlevel 1 goto :not_production
for /f "usebackq tokens=1,* delims==" %%a in ("%MM_ANSWERS%") do set "MM_%%a=%%b"
rem git read into a file, not in a for: a git error (dubious ownership) would
rem be lost there, and said as not being on main.
call git rev-parse --abbrev-ref HEAD > "%MM_ANSWERS%.branch"
if errorlevel 1 goto :git_unreadable
for /f "usebackq delims=" %%b in ("%MM_ANSWERS%.branch") do set "MM_BRANCH=%%b"
if /i not "%MM_BRANCH%"=="main" goto :not_on_main
rem git diff --quiet: 1 when a file differs, 128 when git cannot read the folder.
call git diff --quiet HEAD --
if errorlevel 2 goto :git_unreadable
if errorlevel 1 goto :code_modified

rem 2. The changes committed in the development folder.
echo Recherche des changements dans le dossier de developpement (git fetch origin)...
call git fetch origin
if errorlevel 1 goto :fetch_failed
for /f "delims=" %%h in ('git rev-parse HEAD') do set "MM_PREVIOUS=%%h"
for /f "delims=" %%h in ('git rev-parse --short HEAD') do set "MM_PREVIOUS_SHORT=%%h"
set "MM_NEW_COMMITS="
for /f "delims=" %%n in ('git rev-list --count HEAD..origin/main') do set "MM_NEW_COMMITS=%%n"
if not defined MM_NEW_COMMITS goto :fetch_failed
if "%MM_NEW_COMMITS%"=="0" goto :nothing_new
call git merge-base --is-ancestor HEAD origin/main
if errorlevel 1 goto :diverged_histories
echo(
echo Version en ligne : %MM_PREVIOUS_SHORT%. Changements a mettre en ligne (%MM_NEW_COMMITS%) :
call git --no-pager log --oneline HEAD..origin/main
echo(
choice /C ON /N /M "Deployer ces changements ? (O/N) "
if not "%ERRORLEVEL%"=="1" goto :cancelled

rem 3. Nothing may run in a tenant: stopping the server would cut it off.
echo(
echo Travaux en cours dans les espaces (manage.py running_jobs) :
"%MM_PYTHON%" manage.py running_jobs
if errorlevel 1 goto :jobs_running
rem The task MarginMate must start THIS folder's start_production.cmd: created
rem before the two copies, it would start the development folder's, and the
rem restart would serve the other code. [char]34 and not a double quote, which
rem would close cmd's.
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "$t = Get-ScheduledTask -TaskName '%MM_TASK%' -ErrorAction SilentlyContinue; if (-not $t) { exit 0 }; $want = [IO.Path]::GetFullPath('%MM_APP%start_production.cmd'); foreach ($a in @($t.Actions)) { if ($a.Execute -and [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($a.Execute.Trim().Trim([char]34))) -ieq $want) { exit 0 } }; exit 1"
if errorlevel 1 goto :task_elsewhere

rem 4. Stopping the server. From here on, etat.txt keeps what going back needs,
rem even if this window is closed: the next deployment reads it. Its keys stay
rem French (ancien, donnees, sauvegarde, etape): the deploy.cmd of another
rem version reads what this one wrote, and the owner is shown the file.
>"%MM_STATE%" echo ancien=%MM_PREVIOUS%
>>"%MM_STATE%" echo donnees=%MM_DATA%
set "MM_STEP=l'arret du serveur"
>>"%MM_STATE%" echo etape=%MM_STEP%
rem Was it running? A failed backup restarts it as it was.
call :wait_for_port %MM_PORT% listening 0
if not errorlevel 1 set "MM_WAS_RUNNING=1"
echo(
echo Arret du serveur...
schtasks /end /tn "%MM_TASK%" >nul 2>&1
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %MM_PORT% -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
call :wait_for_port %MM_PORT% free 30
if errorlevel 1 goto :cannot_stop
echo Serveur arrete.
rem A job started between the check and the stop has just been cut off: its
rem last sign of life is still fresh, running_jobs sees it RUNNING.
echo Travaux coupes par l'arret (manage.py running_jobs) :
"%MM_PYTHON%" manage.py running_jobs
if errorlevel 1 goto :job_cut_off

rem 5. The backup, the server stopped.
set "MM_STEP=la sauvegarde (manage.py backup_data)"
>>"%MM_STATE%" echo etape=%MM_STEP%
echo(
del "%MM_BACKUP_PATH_FILE%" >nul 2>&1
"%MM_PYTHON%" manage.py backup_data --chemin-dans "%MM_BACKUP_PATH_FILE%"
if errorlevel 1 goto :backup_failure
for /f "usebackq delims=" %%s in ("%MM_BACKUP_PATH_FILE%") do set "MM_BACKUP=%%s"
del "%MM_BACKUP_PATH_FILE%" >nul 2>&1
>>"%MM_STATE%" echo sauvegarde=%MM_BACKUP%
rem Nobody restarted the server meanwhile?
call :wait_for_port %MM_PORT% free 0
if errorlevel 1 goto :server_came_back

rem 6. The new code.
set "MM_STEP=la mise a jour du code (git merge --ff-only origin/main)"
>>"%MM_STATE%" echo etape=%MM_STEP%
echo(
echo Mise a jour du code...
call git merge --ff-only origin/main
if errorlevel 1 goto :merge_failure
for /f "delims=" %%h in ('git rev-parse --short HEAD') do set "MM_NEW_SHORT=%%h"

rem 7. What the new code asks for, then its checks. uv installs what uv.lock
rem lists and removes the rest, the development tools included.
set "MM_STEP=l'installation des dependances (uv sync --locked --no-dev)"
>>"%MM_STATE%" echo etape=%MM_STEP%
echo(
echo Dependances (uv sync --locked --no-dev)...
call uv sync --locked --no-dev
if errorlevel 1 goto :failure_after_merge
set "MM_STEP=les migrations (manage.py migrate_tenants)"
>>"%MM_STATE%" echo etape=%MM_STEP%
echo(
echo Migrations (manage.py migrate_tenants)...
"%MM_PYTHON%" manage.py migrate_tenants
if errorlevel 1 goto :failure_after_merge
set "MM_STEP=les verifications du serveur (manage.py serve --verifier)"
>>"%MM_STATE%" echo etape=%MM_STEP%
echo(
"%MM_PYTHON%" manage.py serve --verifier
if errorlevel 1 goto :failure_after_merge

rem 8. The restart.
set "MM_STEP=la relance du serveur"
>>"%MM_STATE%" echo etape=%MM_STEP%
echo(
echo Relance du serveur...
call :start_server
call :wait_for_port %MM_PORT% listening 120
if errorlevel 1 goto :server_silent
echo(
echo Deploye : %MM_PREVIOUS_SHORT%..%MM_NEW_SHORT%
echo Le serveur ecoute sur 127.0.0.1:%MM_PORT% avec la nouvelle version.
echo Sauvegarde faite juste avant : "%MM_BACKUP%"
set "MM_CODE=0"
goto :finish

rem ---------------------------------------------------------------------------
rem Refusals: nothing was done, the server was not touched.

:folder_not_found
echo Le dossier de deploy.cmd est introuvable : "%MM_APP%". Rien n'a ete fait.
goto :finish

:no_venv
echo REFUS : le dossier .venv est introuvable dans "%MM_APP%" (DEPLOY.md, section 10).
echo Rien n'a ete fait.
goto :finish

:not_a_clone
echo REFUS : ce dossier n'est pas un clone git. deploy.cmd se lance dans la copie de
echo production, C:\MarginMate\app (DEPLOY.md, section 10). Rien n'a ete fait.
goto :finish

:no_git
echo REFUS : git est introuvable (installez Git for Windows). Rien n'a ete fait.
goto :finish

:no_uv
rem A deployment left half way comes first: what it noted says how to go back.
if exist "%MM_LOCK%\" goto :already_running
echo(
echo REFUS : uv ne repond pas. deploy.cmd installe les dependances avec uv (DEPLOY.md,
echo section 10.6) : installez mise, puis, dans une NOUVELLE invite de commandes :
echo   cd /d "%MM_APP%"
echo   mise install
echo   uv --version
echo uv --version doit afficher un numero de version. Rien n'a ete fait.
goto :finish

:already_running
if not exist "%MM_LOCK%\" goto :mark_failed
echo REFUS : une autre mise en ligne a laisse sa marque, le dossier
echo   "%MM_LOCK%"
echo Un deploy.cmd tourne encore dans une autre fenetre, ou s'est arrete en cours de
echo route : echec apres la mise a jour du code, ou fenetre fermee.
if not exist "%MM_STATE%" goto :already_running_without_state
echo(
echo Ce qu'elle a note (etat.txt) :
type "%MM_STATE%"
rem Each line key=value of etat.txt becomes MM_NOTE_key. The keys stay French:
rem the deploy.cmd of another version may have written the file.
for /f "usebackq tokens=1,* delims==" %%a in ("%MM_STATE%") do set "MM_NOTE_%%a=%%b"
set "MM_PREVIOUS=%MM_NOTE_ANCIEN%"
set "MM_DATA=%MM_NOTE_DONNEES%"
set "MM_BACKUP=%MM_NOTE_SAUVEGARDE%"
set "MM_STEP=%MM_NOTE_ETAPE%"
echo(
echo Si une autre fenetre deploy.cmd est encore ouverte, ne faites rien ici : attendez
echo qu'elle finisse et lisez-la. Sinon, cette mise en ligne s'est arretee pendant
echo %MM_STEP%, apres avoir arrete le serveur : le site est peut-etre hors ligne.
goto :rollback_instructions

:already_running_without_state
echo(
echo Elle n'a rien note : elle n'avait pas encore arrete le serveur. Si aucune autre
echo fenetre deploy.cmd n'est ouverte, effacez cette marque, puis relancez deploy.cmd :
echo   rmdir /s /q "%MM_LOCK%"
echo Rien n'a ete fait.
goto :finish

:mark_failed
echo REFUS : impossible de creer le dossier
echo   "%MM_LOCK%"
echo (droits sur le dossier .git ? relancez deploy.cmd en administrateur). Rien n'a ete fait.
goto :finish

:not_production
echo(
echo REFUS : ce dossier n'est pas la copie de production (la raison est ci-dessus).
echo deploy.cmd se lance dans C:\MarginMate\app. Rien n'a ete fait.
goto :finish

:git_unreadable
set "MM_APP_GIT=%MM_APP:\=/%"
set "MM_APP_GIT=%MM_APP_GIT:~0,-1%"
echo(
echo REFUS : git ne lit pas ce dossier (message ci-dessus). S'il parle de dubious ownership
echo (le dossier a ete cree par un autre compte, un administrateur), autorisez-le, dans une
echo invite de commandes :
echo   git config --global --add safe.directory "%MM_APP_GIT%"
echo Rien n'a ete fait.
goto :finish

:not_on_main
echo REFUS : la copie de production n'est pas sur la branche main (elle est sur "%MM_BRANCH%").
echo Dans une invite de commandes, dans "%MM_APP%" : git checkout main. Rien n'a ete fait.
goto :finish

:code_modified
echo REFUS : des fichiers du code ont ete modifies dans "%MM_APP%" :
call git --no-pager status --short --untracked-files=no
echo Cette copie ne se modifie jamais a la main : faites la modification dans le dossier
echo de developpement. Pour jeter ces modifications : git checkout -- . (dans ce dossier).
echo Rien n'a ete fait.
goto :finish

:fetch_failed
echo REFUS : impossible de lire le dossier de developpement (git fetch origin, lignes
echo ci-dessus). Le serveur n'a pas ete touche.
goto :finish

:diverged_histories
echo REFUS : la version en ligne contient des commits que la branche main du dossier de
echo developpement n'a pas. Une mise en ligne ne fait qu'avancer : rien n'a ete fait.
goto :finish

:nothing_new
call :wait_for_port %MM_PORT% listening 0
if errorlevel 1 goto :offline
echo Rien de nouveau : la version en ligne (%MM_PREVIOUS_SHORT%) est deja la derniere de main
echo dans le dossier de developpement. Seuls les changements enregistres (git commit) sur
echo main sont mis en ligne. Le serveur n'a pas ete touche.
set "MM_CODE=0"
goto :finish

:offline
echo(
echo ================================================================================
echo ATTENTION : rien n'ecoute sur 127.0.0.1:%MM_PORT%, le site est hors ligne.
echo Le code de ce dossier (%MM_PREVIOUS_SHORT%) est deja la derniere version de main : il n'y
echo a rien a mettre en ligne, et deploy.cmd n'a rien touche. Si une mise en ligne vient
echo d'echouer, revenez d'abord a la version d'avant, donnee par la fenetre de cet echec
echo (DEPLOY.md, section 10.4), dans une invite de commandes :
echo(
echo   cd /d "%MM_APP%"
echo   git reset --hard VERSION-D-AVANT
echo   uv sync --locked --no-dev
echo(
echo Puis verifiez ce dossier et relancez le serveur :
echo(
echo   .venv\Scripts\python.exe manage.py serve --verifier
echo   schtasks /run /tn MarginMate
echo(
echo ou double-cliquez sur start_production.cmd.
echo ================================================================================
goto :finish

:cancelled
echo Mise en ligne annulee : rien n'a ete fait, le serveur n'a pas ete touche.
goto :finish

:jobs_running
echo(
echo REFUS : un travail tourne encore (ci-dessus), ou les espaces n'ont pas pu etre
echo verifies. Le serveur n'a pas ete arrete : attendez la fin de la recuperation ou de
echo l'import, puis relancez deploy.cmd.
goto :finish

:task_elsewhere
echo(
echo REFUS : la tache %MM_TASK% ne lance pas %MM_APP%start_production.cmd : corrigez son
echo action (DEPLOY.md section 7). Rien n'a ete fait, le serveur n'a pas ete touche.
goto :finish

:cannot_stop
echo(
echo ECHEC : le serveur ne s'est pas arrete, ou le port 127.0.0.1:%MM_PORT% ne se lit pas.
echo Le code et les donnees n'ont pas change. Si la tache MarginMate s'execute avec les
echo autorisations maximales, relancez deploy.cmd en administrateur. Verifiez que le site
echo repond ; sinon, relancez le serveur : schtasks /run /tn MarginMate
goto :finish

rem ---------------------------------------------------------------------------
rem Failures before the new code: the server is restarted as it was.

:job_cut_off
echo(
echo ATTENTION : ce travail (ci-dessus) a commence entre la verification et l'arret du
echo serveur et vient d'etre coupe, ou les espaces ne se lisent plus. Un travail coupe
echo apparaitra interrompu : il se relance, ou se reprend, depuis sa page. Rien n'a ete
echo mis a jour.
call :restart_if_running
echo Attendez qu'il soit repris et termine, ou quelques minutes qu'il soit considere
echo comme interrompu, puis relancez deploy.cmd.
goto :finish

:backup_failure
echo(
echo ECHEC de la sauvegarde (lignes ci-dessus) : le code et les donnees n'ont pas change.
call :restart_if_running
goto :finish

:server_came_back
echo(
echo ARRET : un serveur ecoute de nouveau sur 127.0.0.1:%MM_PORT% pendant la mise en ligne
echo (la tache MarginMate s'est-elle relancee d'elle-meme ?). Le code n'a pas change et le
echo serveur tourne sur la version d'avant. Sauvegarde faite : "%MM_BACKUP%"
goto :finish

:merge_failure
echo(
echo ECHEC de la mise a jour du code (lignes ci-dessus). Retour au code d'avant...
call git reset --hard %MM_PREVIOUS%
if errorlevel 1 goto :failure_after_merge
echo Le code est revenu a %MM_PREVIOUS_SHORT%, les donnees n'ont pas change.
call :restart_if_running
goto :finish

rem ---------------------------------------------------------------------------
rem Failures once the new code is in place: NOTHING restarts the server.
rem Restarted, the new code would run on half-migrated data, or the old code on
rem migrated data. The mark of step 0 stays: the next deployment refuses and
rem says again how to go back, instead of saying Rien de nouveau.

:failure_after_merge
set "MM_RELEASE=0"
echo(
echo ================================================================================
echo ECHEC pendant %MM_STEP%.
echo Le serveur N'A PAS ete relance : le site affiche une erreur Cloudflare.
goto :rollback_instructions

:server_silent
set "MM_RELEASE=0"
echo(
echo ================================================================================
echo Le code est a jour (%MM_PREVIOUS_SHORT%..%MM_NEW_SHORT%), mais rien n'ecoute sur
echo 127.0.0.1:%MM_PORT% deux minutes apres la relance : lisez la fenetre du serveur, et
echo le journal (dossier logs des donnees). S'il finit par repondre, la mise en ligne est
echo faite : effacez seulement la marque (rmdir, ci-dessous). S'il faut revenir en arriere :
goto :rollback_instructions

rem The way back, also printed for a mark a deployment left half way.
:rollback_instructions
echo(
echo Pour revenir a la version d'avant, ouvrez une invite de commandes et tapez :
echo(
echo   cd /d "%MM_APP%"
echo   git reset --hard %MM_PREVIOUS%
echo   uv sync --locked --no-dev
echo(
if not defined MM_BACKUP goto :rollback_without_backup
echo Si l'echec a eu lieu aux migrations ou apres, remettez aussi les donnees de la
echo sauvegarde faite juste avant, serveur arrete :
echo   "%MM_BACKUP%"
echo en renommant le dossier des donnees (rien n'est efface), puis en recopiant celui
echo de la sauvegarde :
echo(
echo   move "%MM_DATA%" "%MM_DATA%.echec"
echo   robocopy "%MM_BACKUP%\data" "%MM_DATA%" /E
echo(
goto :rollback_restart

:rollback_without_backup
echo Aucune sauvegarde n'a ete notee : la mise en ligne s'est arretee avant la mise a jour
echo du code, et n'a pas touche aux donnees. Un dossier de sauvegarde date de ce moment,
echo sans manifest.json, est une sauvegarde inachevee : supprimez-le.
echo(

:rollback_restart
echo Puis relancez le serveur :
echo   schtasks /run /tn MarginMate
echo ou double-cliquez sur start_production.cmd. Effacez ensuite la marque de cette mise
echo en ligne inachevee, qui fait refuser deploy.cmd :
echo   rmdir /s /q "%MM_LOCK%"
echo Une fois revenu a la version d'avant (git reset ci-dessus), relancez deploy.cmd : il
echo retrouvera les memes changements.
echo ================================================================================
goto :finish

:finish
if "%MM_RELEASE%"=="1" rmdir /s /q "%MM_LOCK%" 2>nul
if "%MM_RELEASE%"=="1" if exist "%MM_LOCK%\" echo ATTENTION : "%MM_LOCK%" n'a pas pu etre efface : effacez-le avant la prochaine mise en ligne.
if defined MM_ANSWERS del "%MM_ANSWERS%" >nul 2>&1
if defined MM_ANSWERS del "%MM_ANSWERS%.branch" >nul 2>&1
if defined MM_BACKUP_PATH_FILE del "%MM_BACKUP_PATH_FILE%" >nul 2>&1
echo(
pause
if defined MM_CODE_PAGE chcp %MM_CODE_PAGE% >nul
exit /b %MM_CODE%

rem ---------------------------------------------------------------------------
rem Subroutines (call).

:wait_for_port
rem Arguments: the port, free or listening, the number of seconds to wait at most.
rem Returns 0 when the port is in that state, 1 otherwise, 2 when it cannot be read.
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "for ($i = 0; $i -le %3; $i++) { try { $c = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %1 -State Listen -ErrorAction SilentlyContinue) } catch { exit 2 }; if (('%2' -eq 'listening') -eq ($c.Count -gt 0)) { exit 0 }; if ($i -lt %3) { Start-Sleep -Seconds 1 } }; exit 1"
exit /b %ERRORLEVEL%

:start_server
schtasks /query /tn "%MM_TASK%" >nul 2>&1
if errorlevel 1 goto :start_in_a_window
schtasks /run /tn "%MM_TASK%" >nul
if errorlevel 1 goto :start_in_a_window
echo Serveur lance par la tache planifiee %MM_TASK%.
exit /b 0

:start_in_a_window
start "MarginMate" cmd /k start_production.cmd
echo Serveur lance dans une nouvelle fenetre (start_production.cmd).
exit /b 0

:restart_if_running
if "%MM_WAS_RUNNING%"=="1" goto :restart
echo Le serveur ne tournait pas avant la mise en ligne : il n'est pas relance.
exit /b 0

:restart
echo Relance du serveur sur la version d'avant...
call :start_server
call :wait_for_port %MM_PORT% listening 120
if errorlevel 1 echo Rien n'ecoute encore sur 127.0.0.1:%MM_PORT% : lisez la fenetre du serveur.
exit /b 0
