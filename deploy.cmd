@echo off
rem MarginMate : met en ligne la derniere version du code (DEPLOY.md, section 10).
rem
rem Se lance d'un double-clic dans la copie de PRODUCTION, C:\MarginMate\app : un
rem clone git dont le depot "origin" est le dossier de developpement. Elle y prend
rem ce qui a ete enregistre (git commit) sur la branche main, et dans l'ordre :
rem   0. une seule mise en ligne a la fois : elle cree le dossier
rem      .git\marginmate-deploy (mkdir, qui echoue quand il existe deja), y note ou
rem      elle en est (etat.txt), et ne l'efface qu'une fois finie, ou apres un echec
rem      qui a remis le serveur tel qu'il etait. Laisse la (echec apres la mise a jour
rem      du code, fenetre fermee en cours de route), il fait refuser la mise en ligne
rem      suivante, qui affiche ce qui a ete note et comment revenir en arriere ;
rem   1. refuse de tourner ailleurs qu'en production : un .env qui dit
rem      DJANGO_DEBUG=True, ou pas MARGINMATE_HTTPS=1, est celui du developpement ;
rem   2. git fetch origin, montre les changements, demande confirmation. Rien de
rem      nouveau : verifie que le site ecoute, et dit s'il est hors ligne ;
rem   3. refuse tant qu'un travail tourne dans un espace (manage.py running_jobs), et
rem      quand la tache planifiee MarginMate lance un autre dossier que celui-ci ;
rem   4. arrete le serveur : la tache planifiee MarginMate, puis ce qui ecoute
rem      sur 127.0.0.1:8765. Un travail commence entre la verification et l'arret :
rem      le serveur est relance tel qu'il etait, la mise en ligne refusee ;
rem   5. sauvegarde les donnees (manage.py backup_data). Si elle echoue : le
rem      serveur est relance tel qu'il etait, rien n'a change ;
rem   6. git merge --ff-only origin/main. Si elle echoue : retour au code d'avant,
rem      le serveur est relance ;
rem   7. pip install, manage.py migrate_tenants, manage.py serve --verifier. Si une
rem      etape echoue : le serveur N'EST PAS relance, la marque de l'etape 0 reste,
rem      et la fenetre dit comment revenir en arriere ;
rem   8. relance le serveur et attend qu'il ecoute sur 127.0.0.1:8765.
rem
rem Ce fichier fait partie du code que git met a jour, et cmd.exe lit un .cmd
rem ligne par ligne sur le disque pendant qu'il tourne : il se recopie d'abord
rem dans le dossier temporaire et continue depuis cette copie.
rem Pas d'accent dans ce fichier : cmd.exe le lit dans la page de code de la console.
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--depuis-la-copie" goto :depuis_la_copie
set "MM_COPIE=%TEMP%\marginmate-deploy-%RANDOM%%RANDOM%.cmd"
copy /y "%~f0" "%MM_COPIE%" >nul
if errorlevel 1 goto :copie_impossible
rem Sans call : la main passe a la copie et ne revient jamais ici.
"%MM_COPIE%" --depuis-la-copie "%~dp0"

:copie_impossible
echo Impossible de recopier deploy.cmd dans le dossier temporaire : rien n'a ete fait.
pause
exit /b 1

:depuis_la_copie
set "MM_APP=%~2"
set "MM_CODE=1"
cd /d "%MM_APP%"
if errorlevel 1 goto :dossier_introuvable
set "MM_PORT=8765"
set "MM_TACHE=MarginMate"
set "MM_PYTHON=.venv\Scripts\python.exe"
set "MM_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "MM_INFOS=%TEMP%\marginmate-deploy-infos-%RANDOM%%RANDOM%.txt"
set "MM_CHEMIN_SAUVEGARDE=%TEMP%\marginmate-deploy-sauvegarde-%RANDOM%%RANDOM%.txt"
set "MM_VERROU=%MM_APP%.git\marginmate-deploy"
set "MM_ETAT=%MM_APP%.git\marginmate-deploy\etat.txt"
set "MM_LIBERER=0"
set "MM_BRANCHE="
set "MM_SAUVEGARDE="
set "MM_ETAIT_LANCE=0"
set "MM_ETAPE=la preparation"
for /f "tokens=2 delims=:." %%c in ('chcp') do set "MM_PAGE_DE_CODE=%%c"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
title MarginMate - mise en ligne
echo MarginMate - mise en ligne d'une nouvelle version
echo Dossier : "%MM_APP%"
echo(

if not exist "%MM_PYTHON%" goto :pas_de_venv
if not exist ".git\" goto :pas_un_clone
where git >nul 2>&1
if errorlevel 1 goto :pas_de_git

rem 0. Une seule mise en ligne a la fois. mkdir est atomique : de deux fenetres, une
rem seule cree le dossier. Apres la verification du clone : mkdir creerait un .git
rem absent. Jamais un fichier tenu ouvert (9 et une redirection) : cmd le passerait a la
rem fenetre du serveur que start ouvre, qui le garderait toute sa vie.
mkdir "%MM_VERROU%" 2>nul || goto :deja_en_cours
set "MM_LIBERER=1"

rem 1. La copie de production, et rien d'autre.
"%MM_PYTHON%" -c "import sys; from accounts import deployment; sys.exit(deployment.main())" production > "%MM_INFOS%"
if errorlevel 1 goto :pas_la_production
for /f "usebackq tokens=1,* delims==" %%a in ("%MM_INFOS%") do set "MM_%%a=%%b"
rem git lu dans un fichier, pas dans un for : une erreur de git (dubious ownership) y
rem serait perdue et dite pas sur main.
call git rev-parse --abbrev-ref HEAD > "%MM_INFOS%.branche"
if errorlevel 1 goto :git_illisible
for /f "usebackq delims=" %%b in ("%MM_INFOS%.branche") do set "MM_BRANCHE=%%b"
if /i not "%MM_BRANCHE%"=="main" goto :pas_sur_main
rem git diff --quiet : 1 quand un fichier differe, 128 quand git ne lit pas le dossier.
call git diff --quiet HEAD --
if errorlevel 2 goto :git_illisible
if errorlevel 1 goto :code_modifie

rem 2. Les changements enregistres dans le dossier de developpement.
echo Recherche des changements dans le dossier de developpement (git fetch origin)...
call git fetch origin
if errorlevel 1 goto :fetch_impossible
for /f "delims=" %%h in ('git rev-parse HEAD') do set "MM_ANCIEN=%%h"
for /f "delims=" %%h in ('git rev-parse --short HEAD') do set "MM_ANCIEN_COURT=%%h"
set "MM_NOUVEAUX="
for /f "delims=" %%n in ('git rev-list --count HEAD..origin/main') do set "MM_NOUVEAUX=%%n"
if not defined MM_NOUVEAUX goto :fetch_impossible
if "%MM_NOUVEAUX%"=="0" goto :rien_de_nouveau
call git merge-base --is-ancestor HEAD origin/main
if errorlevel 1 goto :historiques_divergents
echo(
echo Version en ligne : %MM_ANCIEN_COURT%. Changements a mettre en ligne (%MM_NOUVEAUX%) :
call git --no-pager log --oneline HEAD..origin/main
echo(
choice /C ON /N /M "Deployer ces changements ? (O/N) "
if not "%ERRORLEVEL%"=="1" goto :annule

rem 3. Rien ne doit tourner dans un espace : l'arret du serveur le couperait net.
echo(
echo Travaux en cours dans les espaces (manage.py running_jobs) :
"%MM_PYTHON%" manage.py running_jobs
if errorlevel 1 goto :travail_en_cours
rem La tache MarginMate doit lancer le start_production.cmd de CE dossier : creee avant
rem les deux copies, elle lancerait celui du dossier de developpement, et la relance
rem servirait l'autre code. [char]34 et non un guillemet : il fermerait ceux de cmd.
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "$t = Get-ScheduledTask -TaskName '%MM_TACHE%' -ErrorAction SilentlyContinue; if (-not $t) { exit 0 }; $want = [IO.Path]::GetFullPath('%MM_APP%start_production.cmd'); foreach ($a in @($t.Actions)) { if ($a.Execute -and [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($a.Execute.Trim().Trim([char]34))) -ieq $want) { exit 0 } }; exit 1"
if errorlevel 1 goto :tache_ailleurs

rem 4. L'arret du serveur. A partir d'ici, etat.txt garde ce qu'il faut pour revenir en
rem arriere, meme si cette fenetre est fermee : la mise en ligne suivante le lit.
>"%MM_ETAT%" echo ancien=%MM_ANCIEN%
>>"%MM_ETAT%" echo donnees=%MM_DONNEES%
set "MM_ETAPE=l'arret du serveur"
>>"%MM_ETAT%" echo etape=%MM_ETAPE%
rem Tournait-il ? Une sauvegarde ratee le relance tel qu'il etait.
call :attendre %MM_PORT% ecoute 0
if not errorlevel 1 set "MM_ETAIT_LANCE=1"
echo(
echo Arret du serveur...
schtasks /end /tn "%MM_TACHE%" >nul 2>&1
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %MM_PORT% -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
call :attendre %MM_PORT% libre 30
if errorlevel 1 goto :arret_impossible
echo Serveur arrete.
rem Un travail commence entre la verification et l'arret vient d'etre coupe : son dernier
rem signe de vie est encore frais, running_jobs le voit EN COURS.
echo Travaux coupes par l'arret (manage.py running_jobs) :
"%MM_PYTHON%" manage.py running_jobs
if errorlevel 1 goto :travail_coupe

rem 5. La sauvegarde, serveur arrete.
set "MM_ETAPE=la sauvegarde (manage.py backup_data)"
>>"%MM_ETAT%" echo etape=%MM_ETAPE%
echo(
del "%MM_CHEMIN_SAUVEGARDE%" >nul 2>&1
"%MM_PYTHON%" manage.py backup_data --chemin-dans "%MM_CHEMIN_SAUVEGARDE%"
if errorlevel 1 goto :echec_sauvegarde
for /f "usebackq delims=" %%s in ("%MM_CHEMIN_SAUVEGARDE%") do set "MM_SAUVEGARDE=%%s"
del "%MM_CHEMIN_SAUVEGARDE%" >nul 2>&1
>>"%MM_ETAT%" echo sauvegarde=%MM_SAUVEGARDE%
rem Personne n'a relance le serveur entre-temps ?
call :attendre %MM_PORT% libre 0
if errorlevel 1 goto :serveur_revenu

rem 6. Le nouveau code.
set "MM_ETAPE=la mise a jour du code (git merge --ff-only origin/main)"
>>"%MM_ETAT%" echo etape=%MM_ETAPE%
echo(
echo Mise a jour du code...
call git merge --ff-only origin/main
if errorlevel 1 goto :echec_fusion
for /f "delims=" %%h in ('git rev-parse --short HEAD') do set "MM_NOUVEAU_COURT=%%h"

rem 7. Ce que le nouveau code demande, puis ses verifications.
set "MM_ETAPE=l'installation des dependances (pip install -r requirements.txt)"
>>"%MM_ETAT%" echo etape=%MM_ETAPE%
echo(
echo Dependances (pip install -r requirements.txt)...
"%MM_PYTHON%" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :echec_apres_fusion
set "MM_ETAPE=les migrations (manage.py migrate_tenants)"
>>"%MM_ETAT%" echo etape=%MM_ETAPE%
echo(
echo Migrations (manage.py migrate_tenants)...
"%MM_PYTHON%" manage.py migrate_tenants
if errorlevel 1 goto :echec_apres_fusion
set "MM_ETAPE=les verifications du serveur (manage.py serve --verifier)"
>>"%MM_ETAT%" echo etape=%MM_ETAPE%
echo(
"%MM_PYTHON%" manage.py serve --verifier
if errorlevel 1 goto :echec_apres_fusion

rem 8. La relance.
set "MM_ETAPE=la relance du serveur"
>>"%MM_ETAT%" echo etape=%MM_ETAPE%
echo(
echo Relance du serveur...
call :demarrer_serveur
call :attendre %MM_PORT% ecoute 120
if errorlevel 1 goto :serveur_muet
echo(
echo Deploye : %MM_ANCIEN_COURT%..%MM_NOUVEAU_COURT%
echo Le serveur ecoute sur 127.0.0.1:%MM_PORT% avec la nouvelle version.
echo Sauvegarde faite juste avant : "%MM_SAUVEGARDE%"
set "MM_CODE=0"
goto :fin

rem ---------------------------------------------------------------------------
rem Refus : rien n'a ete fait, le serveur n'a pas ete touche.

:dossier_introuvable
echo Le dossier de deploy.cmd est introuvable : "%MM_APP%". Rien n'a ete fait.
goto :fin

:pas_de_venv
echo REFUS : le dossier .venv est introuvable dans "%MM_APP%" (DEPLOY.md, section 10).
echo Rien n'a ete fait.
goto :fin

:pas_un_clone
echo REFUS : ce dossier n'est pas un clone git. deploy.cmd se lance dans la copie de
echo production, C:\MarginMate\app (DEPLOY.md, section 10). Rien n'a ete fait.
goto :fin

:pas_de_git
echo REFUS : git est introuvable (installez Git for Windows). Rien n'a ete fait.
goto :fin

:deja_en_cours
if not exist "%MM_VERROU%\" goto :marque_impossible
echo REFUS : une autre mise en ligne a laisse sa marque, le dossier
echo   "%MM_VERROU%"
echo Un deploy.cmd tourne encore dans une autre fenetre, ou s'est arrete en cours de
echo route : echec apres la mise a jour du code, ou fenetre fermee.
if not exist "%MM_ETAT%" goto :deja_en_cours_sans_etat
echo(
echo Ce qu'elle a note (etat.txt) :
type "%MM_ETAT%"
for /f "usebackq tokens=1,* delims==" %%a in ("%MM_ETAT%") do set "MM_NOTE_%%a=%%b"
set "MM_ANCIEN=%MM_NOTE_ANCIEN%"
set "MM_DONNEES=%MM_NOTE_DONNEES%"
set "MM_SAUVEGARDE=%MM_NOTE_SAUVEGARDE%"
set "MM_ETAPE=%MM_NOTE_ETAPE%"
echo(
echo Si une autre fenetre deploy.cmd est encore ouverte, ne faites rien ici : attendez
echo qu'elle finisse et lisez-la. Sinon, cette mise en ligne s'est arretee pendant
echo %MM_ETAPE%, apres avoir arrete le serveur : le site est peut-etre hors ligne.
goto :instructions_retour

:deja_en_cours_sans_etat
echo(
echo Elle n'a rien note : elle n'avait pas encore arrete le serveur. Si aucune autre
echo fenetre deploy.cmd n'est ouverte, effacez cette marque, puis relancez deploy.cmd :
echo   rmdir /s /q "%MM_VERROU%"
echo Rien n'a ete fait.
goto :fin

:marque_impossible
echo REFUS : impossible de creer le dossier
echo   "%MM_VERROU%"
echo (droits sur le dossier .git ? relancez deploy.cmd en administrateur). Rien n'a ete fait.
goto :fin

:pas_la_production
echo(
echo REFUS : ce dossier n'est pas la copie de production (la raison est ci-dessus).
echo deploy.cmd se lance dans C:\MarginMate\app. Rien n'a ete fait.
goto :fin

:git_illisible
set "MM_APP_GIT=%MM_APP:\=/%"
set "MM_APP_GIT=%MM_APP_GIT:~0,-1%"
echo(
echo REFUS : git ne lit pas ce dossier (message ci-dessus). S'il parle de dubious ownership
echo (le dossier a ete cree par un autre compte, un administrateur), autorisez-le, dans une
echo invite de commandes :
echo   git config --global --add safe.directory "%MM_APP_GIT%"
echo Rien n'a ete fait.
goto :fin

:pas_sur_main
echo REFUS : la copie de production n'est pas sur la branche main (elle est sur "%MM_BRANCHE%").
echo Dans une invite de commandes, dans "%MM_APP%" : git checkout main. Rien n'a ete fait.
goto :fin

:code_modifie
echo REFUS : des fichiers du code ont ete modifies dans "%MM_APP%" :
call git --no-pager status --short --untracked-files=no
echo Cette copie ne se modifie jamais a la main : faites la modification dans le dossier
echo de developpement. Pour jeter ces modifications : git checkout -- . (dans ce dossier).
echo Rien n'a ete fait.
goto :fin

:fetch_impossible
echo REFUS : impossible de lire le dossier de developpement (git fetch origin, lignes
echo ci-dessus). Le serveur n'a pas ete touche.
goto :fin

:historiques_divergents
echo REFUS : la version en ligne contient des commits que la branche main du dossier de
echo developpement n'a pas. Une mise en ligne ne fait qu'avancer : rien n'a ete fait.
goto :fin

:rien_de_nouveau
call :attendre %MM_PORT% ecoute 0
if errorlevel 1 goto :hors_ligne
echo Rien de nouveau : la version en ligne (%MM_ANCIEN_COURT%) est deja la derniere de main
echo dans le dossier de developpement. Seuls les changements enregistres (git commit) sur
echo main sont mis en ligne. Le serveur n'a pas ete touche.
set "MM_CODE=0"
goto :fin

:hors_ligne
echo(
echo ================================================================================
echo ATTENTION : rien n'ecoute sur 127.0.0.1:%MM_PORT%, le site est hors ligne.
echo Le code de ce dossier (%MM_ANCIEN_COURT%) est deja la derniere version de main : il n'y
echo a rien a mettre en ligne, et deploy.cmd n'a rien touche. Si une mise en ligne vient
echo d'echouer, revenez d'abord a la version d'avant, donnee par la fenetre de cet echec
echo (DEPLOY.md, section 10.4), dans une invite de commandes :
echo(
echo   cd /d "%MM_APP%"
echo   git reset --hard VERSION-D-AVANT
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
echo(
echo Puis verifiez ce dossier et relancez le serveur :
echo(
echo   .venv\Scripts\python.exe manage.py serve --verifier
echo   schtasks /run /tn MarginMate
echo(
echo ou double-cliquez sur start_production.cmd.
echo ================================================================================
goto :fin

:annule
echo Mise en ligne annulee : rien n'a ete fait, le serveur n'a pas ete touche.
goto :fin

:travail_en_cours
echo(
echo REFUS : un travail tourne encore (ci-dessus), ou les espaces n'ont pas pu etre
echo verifies. Le serveur n'a pas ete arrete : attendez la fin de la recuperation ou de
echo l'import, puis relancez deploy.cmd.
goto :fin

:tache_ailleurs
echo(
echo REFUS : la tache %MM_TACHE% ne lance pas %MM_APP%start_production.cmd : corrigez son
echo action (DEPLOY.md section 7). Rien n'a ete fait, le serveur n'a pas ete touche.
goto :fin

:arret_impossible
echo(
echo ECHEC : le serveur ne s'est pas arrete, ou le port 127.0.0.1:%MM_PORT% ne se lit pas.
echo Le code et les donnees n'ont pas change. Si la tache MarginMate s'execute avec les
echo autorisations maximales, relancez deploy.cmd en administrateur. Verifiez que le site
echo repond ; sinon, relancez le serveur : schtasks /run /tn MarginMate
goto :fin

rem ---------------------------------------------------------------------------
rem Echecs avant le nouveau code : le serveur est relance tel qu'il etait.

:travail_coupe
echo(
echo ATTENTION : ce travail (ci-dessus) a commence entre la verification et l'arret du
echo serveur et vient d'etre coupe, ou les espaces ne se lisent plus. Un travail coupe
echo apparaitra interrompu : il se relance, ou se reprend, depuis sa page. Rien n'a ete
echo mis a jour.
call :relancer_si_lance
echo Attendez qu'il soit repris et termine, ou quelques minutes qu'il soit considere
echo comme interrompu, puis relancez deploy.cmd.
goto :fin

:echec_sauvegarde
echo(
echo ECHEC de la sauvegarde (lignes ci-dessus) : le code et les donnees n'ont pas change.
call :relancer_si_lance
goto :fin

:serveur_revenu
echo(
echo ARRET : un serveur ecoute de nouveau sur 127.0.0.1:%MM_PORT% pendant la mise en ligne
echo (la tache MarginMate s'est-elle relancee d'elle-meme ?). Le code n'a pas change et le
echo serveur tourne sur la version d'avant. Sauvegarde faite : "%MM_SAUVEGARDE%"
goto :fin

:echec_fusion
echo(
echo ECHEC de la mise a jour du code (lignes ci-dessus). Retour au code d'avant...
call git reset --hard %MM_ANCIEN%
if errorlevel 1 goto :echec_apres_fusion
echo Le code est revenu a %MM_ANCIEN_COURT%, les donnees n'ont pas change.
call :relancer_si_lance
goto :fin

rem ---------------------------------------------------------------------------
rem Echecs une fois le nouveau code en place : RIEN ne relance le serveur. Relance, le
rem nouveau code tournerait sur des donnees a moitie migrees, ou l'ancien sur des
rem donnees migrees. La marque de l'etape 0 reste : la mise en ligne suivante refuse
rem et redit comment revenir en arriere, au lieu de dire Rien de nouveau.

:echec_apres_fusion
set "MM_LIBERER=0"
echo(
echo ================================================================================
echo ECHEC pendant %MM_ETAPE%.
echo Le serveur N'A PAS ete relance : le site affiche une erreur Cloudflare.
goto :instructions_retour

:serveur_muet
set "MM_LIBERER=0"
echo(
echo ================================================================================
echo Le code est a jour (%MM_ANCIEN_COURT%..%MM_NOUVEAU_COURT%), mais rien n'ecoute sur
echo 127.0.0.1:%MM_PORT% deux minutes apres la relance : lisez la fenetre du serveur, et
echo le journal (dossier logs des donnees). S'il finit par repondre, la mise en ligne est
echo faite : effacez seulement la marque (rmdir, ci-dessous). S'il faut revenir en arriere :
goto :instructions_retour

:instructions_retour
echo(
echo Pour revenir a la version d'avant, ouvrez une invite de commandes et tapez :
echo(
echo   cd /d "%MM_APP%"
echo   git reset --hard %MM_ANCIEN%
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
echo(
if not defined MM_SAUVEGARDE goto :retour_sans_sauvegarde
echo Si l'echec a eu lieu aux migrations ou apres, remettez aussi les donnees de la
echo sauvegarde faite juste avant, serveur arrete :
echo   "%MM_SAUVEGARDE%"
echo en renommant le dossier des donnees (rien n'est efface), puis en recopiant celui
echo de la sauvegarde :
echo(
echo   move "%MM_DONNEES%" "%MM_DONNEES%.echec"
echo   robocopy "%MM_SAUVEGARDE%\data" "%MM_DONNEES%" /E
echo(
goto :retour_relance

:retour_sans_sauvegarde
echo Aucune sauvegarde n'a ete notee : la mise en ligne s'est arretee avant la mise a jour
echo du code, et n'a pas touche aux donnees. Un dossier de sauvegarde date de ce moment,
echo sans manifest.json, est une sauvegarde inachevee : supprimez-le.
echo(

:retour_relance
echo Puis relancez le serveur :
echo   schtasks /run /tn MarginMate
echo ou double-cliquez sur start_production.cmd. Effacez ensuite la marque de cette mise
echo en ligne inachevee, qui fait refuser deploy.cmd :
echo   rmdir /s /q "%MM_VERROU%"
echo Une fois revenu a la version d'avant (git reset ci-dessus), relancez deploy.cmd : il
echo retrouvera les memes changements.
echo ================================================================================
goto :fin

:fin
if "%MM_LIBERER%"=="1" rmdir /s /q "%MM_VERROU%" 2>nul
if "%MM_LIBERER%"=="1" if exist "%MM_VERROU%\" echo ATTENTION : "%MM_VERROU%" n'a pas pu etre efface : effacez-le avant la prochaine mise en ligne.
if defined MM_INFOS del "%MM_INFOS%" >nul 2>&1
if defined MM_INFOS del "%MM_INFOS%.branche" >nul 2>&1
if defined MM_CHEMIN_SAUVEGARDE del "%MM_CHEMIN_SAUVEGARDE%" >nul 2>&1
echo(
pause
if defined MM_PAGE_DE_CODE chcp %MM_PAGE_DE_CODE% >nul
exit /b %MM_CODE%

rem ---------------------------------------------------------------------------
rem Sous-programmes (call).

:attendre
rem Arguments : le port, libre ou ecoute, le nombre de secondes d'attente au plus.
rem Rend 0 quand le port est dans cet etat, 1 sinon, 2 quand il ne se lit pas.
"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "for ($i = 0; $i -le %3; $i++) { try { $c = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %1 -State Listen -ErrorAction SilentlyContinue) } catch { exit 2 }; if (('%2' -eq 'ecoute') -eq ($c.Count -gt 0)) { exit 0 }; if ($i -lt %3) { Start-Sleep -Seconds 1 } }; exit 1"
exit /b %ERRORLEVEL%

:demarrer_serveur
schtasks /query /tn "%MM_TACHE%" >nul 2>&1
if errorlevel 1 goto :demarrer_dans_une_fenetre
schtasks /run /tn "%MM_TACHE%" >nul
if errorlevel 1 goto :demarrer_dans_une_fenetre
echo Serveur lance par la tache planifiee %MM_TACHE%.
exit /b 0

:demarrer_dans_une_fenetre
start "MarginMate" cmd /k start_production.cmd
echo Serveur lance dans une nouvelle fenetre (start_production.cmd).
exit /b 0

:relancer_si_lance
if "%MM_ETAIT_LANCE%"=="1" goto :relancer
echo Le serveur ne tournait pas avant la mise en ligne : il n'est pas relance.
exit /b 0

:relancer
echo Relance du serveur sur la version d'avant...
call :demarrer_serveur
call :attendre %MM_PORT% ecoute 120
if errorlevel 1 echo Rien n'ecoute encore sur 127.0.0.1:%MM_PORT% : lisez la fenetre du serveur.
exit /b 0
