# Mettre MarginMate en ligne

MarginMate tourne sur ce PC Windows et reste joignable depuis Internet à une adresse comme
`https://gestion.<votre-domaine>`, grâce à un **tunnel Cloudflare**. Le programme `cloudflared`
ouvre lui-même une connexion vers Cloudflare, et les visiteurs passent par elle. **Aucun port n'est
ouvert sur la box.**

Dans ce document, remplacez `<votre-domaine>` par votre nom de domaine. Par exemple, si le site
doit répondre à `gestion.monbar.fr`, `gestion.<votre-domaine>` devient `gestion.monbar.fr`.

Le code fournit le serveur de production (`manage.py serve`, lancé par `start_production.cmd`) et
ses vérifications. Le reste vous revient : le compte Cloudflare, les DNS, le tunnel, le fichier
`.env` et les sauvegardes. Ce document le décrit étape par étape.

Le site tourne depuis une copie du code réservée à la production, **`C:\MarginMate\app`**, sur les
données de **`C:\MarginMate\data`**. Le code se modifie ailleurs, dans le dossier de développement,
et se met en ligne par `deploy.cmd` : section 10.

## Avant de commencer

- **Le site vit sur ce PC.** Quand le PC est éteint ou en veille, le site ne répond plus. Dans
  Paramètres > Système > Alimentation, réglez la mise en veille sur « Jamais » (sur secteur).
- **Deux serveurs, deux ports.** Le serveur de production écoute sur `http://127.0.0.1:8765` :
  c'est l'adresse que vise le tunnel. `runserver`, le serveur de développement, garde son port
  habituel, 8000 : il n'est pas fait pour Internet et le tunnel ne le vise jamais.
- **Deux copies, deux `.env`.** Les sections 5 à 9 et 11 parlent de la copie de production,
  `C:\MarginMate\app`, et de son `.env`. Le dossier de développement a le sien (section 10).
- **Sur le PC lui-même**, l'adresse `http://127.0.0.1:8765` reste utilisable. Chrome, Edge et
  Firefox acceptent les cookies sécurisés sur cette adresse locale. Si la connexion n'y tient pas,
  passez par `https://gestion.<votre-domaine>`.
- **Gardez pour vous** le jeton du tunnel (section 3) et le fichier `.env`. Ne les envoyez à
  personne, ni par e-mail, ni dans une conversation, et ne les mettez jamais dans le dépôt.
- **L'ordre compte.** Le nom public du site ne s'ajoute au tunnel qu'**à la fin**, à la section 6,
  étape 6 : une fois `runserver` arrêté, les lignes du `.env` en place, les vérifications passées
  et le serveur de production « En ligne ». Ajouté plus tôt, il enverrait les visiteurs d'Internet
  vers ce qui tourne alors sur le PC.
- **Déjà fait ?** Les sections 1 et 2 (le domaine chez Cloudflare, ses serveurs de noms chez OVH)
  sont peut-être déjà faites : passez-les si Cloudflare indique le domaine comme actif. Si un nom
  d'hôte public a déjà été ajouté au tunnel, **supprimez-le dès maintenant** dans le tableau de bord
  (page du tunnel, « Public Hostname ») : vous le recréerez à la section 6, avec la bonne adresse.

## 1. Cloudflare : ajouter le domaine

1. Créez un compte gratuit sur cloudflare.com.
2. Cliquez sur « Add a domain » et donnez `<votre-domaine>`. Choisissez l'offre gratuite (Free).
3. Cloudflare importe les enregistrements DNS existants. Vérifiez-les avant de continuer, surtout
   les enregistrements **MX** si vos e-mails sont chez OVH. Ils doivent être là, sinon le courrier
   ne passera plus.
4. Cloudflare affiche **deux serveurs de noms** (du genre `xxx.ns.cloudflare.com`). Notez-les.

## 2. OVH : passer les DNS à Cloudflare

1. Dans l'espace client OVH, allez dans Noms de domaine > `<votre-domaine>` > **Serveurs DNS**.
2. Cliquez sur « Modifier les serveurs DNS » et remplacez ceux d'OVH par les deux serveurs de noms
   de Cloudflare.
3. Attendez que Cloudflare indique le domaine comme actif. Cela prend de quelques minutes à 24 h.

Le domaine reste enregistré chez OVH : seule la gestion des DNS passe chez Cloudflare.

## 3. Le tunnel

`cloudflared` est déjà installé sur le PC. Le tunnel se crée dans le tableau de bord de Cloudflare.

1. Ouvrez Cloudflare **Zero Trust**, puis Networks > **Tunnels** > « Create a tunnel ».
2. Choisissez le type **Cloudflared** et nommez le tunnel `marginmate`.
3. Pour l'environnement, choisissez **Windows**. Le tableau de bord affiche une commande de la forme :

   ```
   cloudflared.exe service install <JETON>
   ```

   Le `<JETON>` est le long texte qui suit `install` : c'est la clé du tunnel.
4. Ouvrez une **invite de commandes en administrateur** : menu Démarrer, tapez `cmd`, clic droit,
   « Exécuter en tant qu'administrateur ». Collez-y la commande. Si Windows ne trouve pas
   `cloudflared.exe`, donnez son chemin complet, par exemple
   `"C:\Program Files (x86)\cloudflared\cloudflared.exe" service install <JETON>`.
   Le tunnel devient un **service Windows** : il démarre avec le PC, même sans session ouverte.
   Pour vérifier qu'il tourne : `sc query cloudflared` doit afficher `RUNNING`.
5. **N'ajoutez pas encore de nom d'hôte public** (« Public Hostname » ou « Published application
   routes », selon la version du tableau de bord). Si l'assistant le propose maintenant, quittez-le :
   le tunnel reste enregistré, et le nom s'ajoute plus tard depuis la page du tunnel (section 6,
   étape 6). Si l'assistant ne laisse pas passer cette étape, faites d'abord les sections 4, 5 et 6
   jusqu'à l'étape 5, puis revenez-y.

Le tunnel transmet au serveur l'adresse de chaque visiteur (en-têtes `X-Forwarded-For` et
`CF-Connecting-IP`) et le fait qu'il est venu en HTTPS (`X-Forwarded-Proto`). Le serveur ne croit
ces en-têtes que s'ils viennent du tunnel, c'est-à-dire de 127.0.0.1. C'est ce qui permet au
limiteur de connexions et aux preuves de signature d'enregistrer la vraie adresse du visiteur.

## 4. HTTPS chez Cloudflare

Dans le tableau de bord du domaine, allez dans SSL/TLS > **Edge Certificates** :

- **Always Use HTTPS : On.** Cloudflare renvoie tout visiteur `http://` vers `https://`. Le serveur
  ne le fait pas lui-même.
- Minimum TLS Version : 1.2.
- N'activez **pas** le HSTS de Cloudflare avec « includeSubDomains » ou « preload ». MarginMate
  envoie déjà son propre HSTS, d'une heure au départ, qui ne concerne que `gestion.<votre-domaine>`.

Le mode SSL (« Flexible », « Full »…) ne concerne pas le tunnel : laissez la valeur par défaut.

## 5. Le fichier .env

Ouvrez `C:\MarginMate\app\.env`, le `.env` de la copie de production, dans le Bloc-notes. Il doit
contenir ces lignes. Ajoutez celles qui manquent et corrigez les autres.

```
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=gestion.<votre-domaine>,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://gestion.<votre-domaine>
MARGINMATE_HTTPS=1
MARGINMATE_SITE_URL=https://gestion.<votre-domaine>
```

Ces lignes sont déjà dans votre `.env`. Gardez-les telles quelles :

- `DJANGO_SECRET_KEY` : au moins 50 caractères tirés au hasard. Le serveur refuse de démarrer
  avec une clé trop courte, celle de l'exemple ou une clé « django-insecure ». Pour en générer une :
  `.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`.
  En changer déconnecte tout le monde et annule les codes de signature en cours.
- `MARGINMATE_SIGNING_PASSPHRASE` : elle chiffre les clés de signature. Sans elle, le serveur
  refuse de démarrer.
- `MARGINMATE_TENANTS_ROOT` et `MARGINMATE_ACCOUNTS_DB` : le dossier des espaces et la base des
  comptes, dans `C:\MarginMate\data` : `C:\MarginMate\data\tenants` et
  `C:\MarginMate\data\accounts.sqlite3`.

Deux lignes sont facultatives :

- `MARGINMATE_LOG_DIR` : le dossier du journal. Vide, c'est `logs\`, à côté du dossier des
  espaces, donc `C:\MarginMate\data\logs\`.
- `MARGINMATE_HSTS_SECONDS` : la durée pendant laquelle les navigateurs retiennent le HTTPS. Vide,
  c'est 3600 secondes (une heure). Quand tout marche depuis quelques semaines, passez à
  `31536000` (un an).

Enfin, supprimez la ligne `MARGINMATE_TENANCY` si elle y est encore : elle ne sert plus à rien.

`DJANGO_DEBUG=False` est obligatoire dès que `DJANGO_ALLOWED_HOSTS` nomme le site public. Avec le
mode debug, n'importe qui pourrait voir les réglages du serveur, et `manage.py check`, `runserver`
et `migrate_tenants` refusent de démarrer (accounts.E007). `manage.py serve`, lui, coupe le mode
debug de toute façon.

### Essayer quelque chose

Jamais dans la copie de production : ce qu'un essai enregistre, modifie ou efface l'est pour de
bon, et le serveur de production doit rester le seul à travailler sur `C:\MarginMate\data`.
`runserver` ne tourne **jamais sur les données du site**. Les essais se font dans le dossier de
développement, avec son propre `.env` (le mode debug y est permis, les identifiants y sont vides) et
sa copie des données : section 10.

## 6. Premier démarrage, puis la mise en ligne

1. **Arrêtez `runserver`** s'il tourne : faites Ctrl+C dans sa fenêtre (un assistant de code peut
   aussi en avoir lancé un pour un aperçu : fermez-le). Le serveur de production doit être le seul
   à travailler sur les données.
2. Faites une **sauvegarde** (section 8).
3. Vérifiez les réglages sans rien démarrer, dans une invite de commandes ouverte dans
   `C:\MarginMate\app` : `.venv\Scripts\python.exe manage.py serve --verifier`. La dernière ligne
   doit être « Vérifications : tout est en ordre. ». Sinon, le message dit pourquoi et quoi faire
   (voir la section 11).
   - S'il signale des **migrations à appliquer**, lancez, après la sauvegarde :
     `.venv\Scripts\python.exe manage.py migrate_tenants`, puis vérifiez à nouveau.
     Le serveur ne migre jamais rien lui-même.
4. Double-cliquez sur **`start_production.cmd`**, dans `C:\MarginMate\app`. Le serveur refait les
   vérifications (mode debug coupé, clé secrète, noms d'hôte, HTTPS, phrase de passe, migrations),
   puis la fenêtre affiche `En ligne sur http://127.0.0.1:8765/`. Laissez-la ouverte.
5. Sur le PC, ouvrez `http://127.0.0.1:8765` : la page de connexion apparaît.
6. **Maintenant seulement**, publiez le site. Vérifiez d'abord les quatre points : `runserver`
   arrêté, lignes du `.env` en place (section 5), « tout est en ordre » à l'étape 3, et la fenêtre
   de `start_production.cmd` qui affiche « En ligne ». Puis, dans le tableau de bord Cloudflare :
   Zero Trust > Networks > Tunnels > `marginmate` > « Public Hostname » (ou « Published application
   routes »), ajoutez :
   - Subdomain : `gestion`
   - Domain : `<votre-domaine>`
   - Service : type **HTTP**, URL **`127.0.0.1:8765`**

   Mettez bien `127.0.0.1` plutôt que `localhost` (le serveur n'écoute que sur cette adresse), et
   le port `8765`, celui du serveur de production, jamais celui de `runserver`. Un serveur qui
   n'est pas `start_production.cmd` refuse de toute façon ce qui arrive par le tunnel.
7. Ouvrez `https://gestion.<votre-domaine>`, depuis un téléphone en 4G par exemple : la page de
   connexion apparaît.
8. Pour arrêter le serveur, faites **Ctrl+C** dans sa fenêtre. Il affiche « Serveur arrêté. ».
   Répondez `O` si Windows demande « Terminer le programme de commandes ? ». Le site répond alors
   par une erreur Cloudflare jusqu'au prochain démarrage.

## 7. Démarrer le serveur à l'ouverture de la session

Le tunnel démarre déjà tout seul (c'est un service). Pour le serveur, utilisez le Planificateur de
tâches de Windows :

1. Ouvrez le menu Démarrer, tapez « Planificateur de tâches », puis cliquez sur « Créer une tâche… »
   (pas « tâche de base »).
2. Onglet **Général** : nom `MarginMate`. Cochez « N'exécuter que si l'utilisateur est connecté » :
   la fenêtre du serveur s'affichera, avec ses messages.
3. Onglet **Déclencheurs** : Nouveau > « À l'ouverture de session », de votre compte.
4. Onglet **Actions** : Nouveau > « Démarrer un programme ».
   - Programme : `C:\MarginMate\app\start_production.cmd`.
   - « Commencer dans » : `C:\MarginMate\app`.

   C'est la copie de production (section 10), jamais le dossier de développement. `deploy.cmd`
   arrête et relance cette tâche : gardez-lui le nom `MarginMate`. Il refuse de mettre en ligne
   tant qu'elle lance un autre fichier que `C:\MarginMate\app\start_production.cmd` : la relance
   démarrerait alors un autre code.
5. Onglet **Conditions** : décochez « Ne démarrer la tâche que si l'ordinateur est relié au secteur ».
6. Onglet **Paramètres** : décochez « Arrêter la tâche si elle s'exécute plus de ». Ne cochez pas
   « Si la tâche échoue, redémarrer toutes les » : `deploy.cmd` arrête la tâche pendant une mise en
   ligne, et le serveur ne doit pas repartir de lui-même au milieu.

Si le serveur refuse de démarrer, sa fenêtre reste ouverte sur le message d'erreur.

## 8. Sauvegardes

Tout ce qui compte tient dans deux choses :

- le dossier **`C:\MarginMate\data`** : les espaces (leurs bases, leurs fichiers, leurs clés de
  signature), la base des comptes et le journal ;
- le fichier **`C:\MarginMate\app\.env`**, qui contient la clé secrète, la phrase de passe et les
  mots de passe.

Une commande sauvegarde les deux. Dans une invite de commandes :

```
cd /d C:\MarginMate\app
.venv\Scripts\python.exe manage.py backup_data
```

Elle crée un dossier daté dans **`C:\MarginMate\backups`**, par exemple
`C:\MarginMate\backups\2026-10-01_101500`, qui contient :

- `data\` : la copie de `C:\MarginMate\data`. Chaque base est copiée par SQLite lui-même, puis
  vérifiée : intégrité, et le même nombre de lignes dans chaque table ;
- `.env` (sauf avec `--sans-env`) ;
- `manifest.json` : ce qui a été copié, et la version du code.

Le serveur peut tourner pendant ce temps : chaque base est copiée telle qu'elle était à un instant
précis. Un fichier qui disparaît pendant la copie (un téléchargement renommé, un import qui range
ses fichiers, le journal qui passe à `marginmate.log.1`) n'est pas une erreur : la commande le
nomme (« fichier(s) disparu(s) pendant la copie ») et le manifeste le liste. `deploy.cmd` en fait
une avant chaque mise en ligne, serveur arrêté (section 10). Si une sauvegarde échoue, la commande
le dit et renomme le dossier commencé `…-INCOMPLET` : ce n'est pas une sauvegarde, supprimez-le une
fois le problème réglé. Une sauvegarde coupée (fenêtre fermée, panne de courant) garde son nom mais
n'a pas de `manifest.json`, écrit en dernier : elle non plus n'est pas une sauvegarde.

La routine, une fois par semaine au moins :

1. `manage.py backup_data`, comme ci-dessus ;
2. une copie hors du PC, sur un disque externe ou dans un dossier synchronisé. Par exemple :

   ```
   robocopy "C:\MarginMate\backups\2026-10-01_101500" "E:\Sauvegardes\MarginMate\2026-10-01_101500" /E
   ```

   ou directement : `.venv\Scripts\python.exe manage.py backup_data --dest E:\Sauvegardes\MarginMate`.

**Gardez plusieurs dates** : les dernières, et une par mois par exemple. Rien ne les efface :
`C:\MarginMate\backups` grossit à chaque sauvegarde et à chaque mise en ligne, supprimez vous-même
les plus anciennes. Une sauvegarde contient `.env`, donc des secrets : rangez-la à l'abri.

Pour remettre une sauvegarde : arrêtez le serveur, renommez le dossier des données (rien n'est
effacé), recopiez celui de la sauvegarde, puis relancez le serveur :

```
move C:\MarginMate\data C:\MarginMate\data.avant-restauration
robocopy "C:\MarginMate\backups\2026-10-01_101500\data" C:\MarginMate\data /E
```

La page « Données » de chaque espace permet aussi d'en exporter une archive.

## 9. Le journal

Le serveur de production (`manage.py serve`, lancé par `start_production.cmd`) note dans
**`C:\MarginMate\data\logs\marginmate.log`** (ou dans `MARGINMATE_LOG_DIR`) :

- les erreurs 500, avec leur trace ;
- les formulaires refusés (CSRF) ;
- les noms d'hôte forgés ;
- les avertissements de l'application.

Le fichier n'est créé qu'à la première ligne écrite. Au-delà de 5 Mo, il passe à
`marginmate.log.1`, et ainsi de suite, jusqu'à cinq fichiers. Les mêmes lignes s'affichent dans la
fenêtre du serveur.

**Le serveur de production est le seul à écrire ce fichier.** Les autres commandes (`runserver`,
`migrate_tenants`, les commandes de rattrapage…) écrivent leurs erreurs et leurs avertissements
dans leur propre fenêtre seulement, et nulle part ailleurs. Sous Windows, un fichier ouvert par
deux programmes à la fois ne peut pas être renommé : le journal ne pouvait plus passer à
`marginmate.log.1`, et des lignes se perdaient.

Le journal ne contient ni les mots de passe, ni le contenu des formulaires, ni la liste des pages
visitées. Les liens de signature envoyés aux salariés y sont coupés après 4 caractères
(`/personnel/signer/Ab3x…/`), car un lien complet ouvre le relevé d'heures.

Les visiteurs, eux, ne voient jamais de détail technique : une erreur leur affiche une page en
français sans chemin, sans réglage et sans trace.

## 10. Développer et mettre en ligne une modification

MarginMate existe en **deux copies** sur ce PC, chacune avec son code, son `.env` et ses données :

| | Production : le site en ligne | Développement : les modifications |
|---|---|---|
| Code | `C:\MarginMate\app` | `C:\Users\<vous>\Desktop\Bar application gestion\AdminMate` |
| Données | `C:\MarginMate\data` | `C:\Users\<vous>\Desktop\Bar application gestion\data-dev`, une copie |
| Sauvegardes | `C:\MarginMate\backups` | aucune : ses données sont une copie |
| Serveur | `start_production.cmd` (la tâche `MarginMate`), port 8765, celui du tunnel | `runserver`, `http://localhost:8000` |
| `.env` | `DJANGO_DEBUG=False`, `MARGINMATE_HTTPS=1` (section 5) | `DJANGO_DEBUG=True`, sans `MARGINMATE_HTTPS`, sans identifiants |

- **On ne modifie jamais `C:\MarginMate\app` à la main.** Il ne change que par `deploy.cmd`, qui y
  apporte ce qui a été enregistré (`git commit`) sur la branche `main` du dossier de développement :
  un fichier modifié mais pas enregistré ne part pas. Pas besoin de GitHub : la copie de production
  va chercher le code directement dans le dossier de développement.
- **Une session Claude Code s'ouvre toujours dans le dossier de développement**, jamais dans
  `C:\MarginMate` : elle y modifie le code, lance les tests et, pour un aperçu, `runserver` sur
  `data-dev`.
- **`data-dev` contient les vraies données du bar**, copiées : factures, banque, personnel. Elles
  ne vont jamais dans git, ni dans un test, ni dans un exemple.

### 10.1 Mise en place (une seule fois)

Jusqu'ici le site tournait depuis le dossier de développement, sur
`C:\Users\<vous>\Desktop\Bar application gestion\data`. Pour passer aux deux copies :

1. Dans le dossier de développement, enregistrez tout le travail en cours sur la branche `main` :
   `git status` doit dire qu'il ne reste rien à enregistrer. La copie de production ne recevra que
   ce qui est enregistré. Rien n'est envoyé sur GitHub.
2. Arrêtez le serveur : Ctrl+C dans la fenêtre de `start_production.cmd` (et `runserver`, s'il
   tourne).
3. Dans une invite de commandes, créez la copie de production du code et son Python :

   ```
   mkdir C:\MarginMate
   git clone -b main "C:\Users\<vous>\Desktop\Bar application gestion\AdminMate" C:\MarginMate\app
   cd /d C:\MarginMate\app
   py -3.11 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. Déplacez les données, le serveur étant arrêté :

   ```
   move "C:\Users\<vous>\Desktop\Bar application gestion\data" C:\MarginMate\data
   ```

   Ne lancez plus rien dans le dossier de développement (`runserver`, un aperçu, une commande)
   avant l'étape 9 : son `.env` désigne encore l'ancien dossier, qui n'existe plus.

5. Copiez le `.env` du dossier de développement dans la copie de production :

   ```
   copy "C:\Users\<vous>\Desktop\Bar application gestion\AdminMate\.env" C:\MarginMate\app\.env
   ```

   Puis ouvrez `C:\MarginMate\app\.env` dans le Bloc-notes : dans `MARGINMATE_TENANTS_ROOT` et
   `MARGINMATE_ACCOUNTS_DB`, remplacez le chemin de l'ancien dossier `data` par
   `C:\MarginMate\data`, par exemple :

   ```
   MARGINMATE_TENANTS_ROOT="C:/MarginMate/data/tenants"
   MARGINMATE_ACCOUNTS_DB="C:/MarginMate/data/accounts.sqlite3"
   ```

   **Écrivez les chemins avec des barres obliques `/`**, comme ci-dessus. Entre guillemets, le
   fichier `.env` lit `\t` comme une tabulation et `\a` comme un autre caractère invisible :
   `"C:\MarginMate\data\tenants"` désignerait un dossier qui n'existe pas, et le serveur dirait
   « La base des comptes est introuvable ».

   `MARGINMATE_LOG_DIR` reste vide (le journal va dans `C:\MarginMate\data\logs`). Les autres
   lignes restent celles de la section 5.
6. Vérifiez, puis démarrez :

   ```
   cd /d C:\MarginMate\app
   .venv\Scripts\python.exe manage.py serve --verifier
   ```

   La dernière ligne doit être « Vérifications : tout est en ordre. ». Double-cliquez ensuite sur
   `C:\MarginMate\app\start_production.cmd`. Rien ne change chez Cloudflare : le tunnel vise
   toujours le port 8765 de ce PC.
7. La tâche planifiée : section 7, avec `C:\MarginMate\app`. Si elle existe déjà, corrigez son
   action : `deploy.cmd` refuse tant qu'elle lance le `start_production.cmd` d'un autre dossier.
8. Une première sauvegarde, dans `C:\MarginMate\app` :
   `.venv\Scripts\python.exe manage.py backup_data`. Elle arrive dans `C:\MarginMate\backups`.
9. Le `.env` du dossier de développement : ouvrez
   `C:\Users\<vous>\Desktop\Bar application gestion\AdminMate\.env` et mettez-y :

   ```
   DJANGO_DEBUG=True
   DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
   DJANGO_CSRF_TRUSTED_ORIGINS=
   MARGINMATE_HTTPS=
   MARGINMATE_SITE_URL=
   MARGINMATE_TENANTS_ROOT="C:/Users/<vous>/Desktop/Bar application gestion/data-dev/tenants"
   MARGINMATE_ACCOUNTS_DB="C:/Users/<vous>/Desktop/Bar application gestion/data-dev/accounts.sqlite3"
   MARGINMATE_LOG_DIR=
   ```

   Puis **videz les identifiants** : `METRO_EMAIL`, `METRO_PASSWORD`, `INVOICE_EMAIL_ADDRESS`,
   `INVOICE_EMAIL_APP_PASSWORD`, `LADDITION_EMAIL`, `LADDITION_PASSWORD`, `ANTHROPIC_API_KEY`,
   `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, et les variables des espaces clients
   (leurs noms sont dans Achats > Sources). Sans eux, une récupération lancée depuis la copie
   refuse ; avec eux, elle se connecterait pour de vrai à Metro et aux portails. Donnez aussi à la
   copie sa propre `DJANGO_SECRET_KEY` (la section 5 dit comment en tirer une). Gardez
   `MARGINMATE_SIGNING_PASSPHRASE` : les clés de signature copiées en ont besoin pour s'ouvrir.
10. Créez la copie des données : double-cliquez sur `refresh_dev_data.cmd`, dans le dossier de
    développement (section 10.5).

### 10.2 Au quotidien

1. **Modifier**, dans le dossier de développement : vous, ou une session Claude Code ouverte dans
   ce dossier.
2. **Essayer**, sur la copie des données :

   ```
   cd /d "C:\Users\<vous>\Desktop\Bar application gestion\AdminMate"
   .venv\Scripts\python.exe manage.py runserver
   ```

   puis `http://localhost:8000`. Ctrl+C pour arrêter.
3. **Enregistrer** : `git add` puis `git commit`, sur `main`. Seul ce qui est enregistré part en
   ligne.
4. **Mettre en ligne** : double-cliquez sur `C:\MarginMate\app\deploy.cmd`.

### 10.3 Ce que fait `deploy.cmd`

Il travaille dans la copie de production, dans cet ordre, chaque étape seulement si la précédente
a réussi :

1. **Une seule mise en ligne à la fois** : il pose une marque, le dossier
   `C:\MarginMate\app\.git\marginmate-deploy`, et refuse de démarrer si elle est déjà là (une autre
   fenêtre `deploy.cmd` ouverte, ou une mise en ligne restée à moitié : section 10.4). Il y note où
   il en est (`etat.txt`) et l'efface en finissant.
2. **Refuse de tourner ailleurs qu'en production** : un `.env` qui dit `DJANGO_DEBUG=True`, ou qui
   ne dit pas `MARGINMATE_HTTPS=1`, est celui du dossier de développement. Il refuse aussi une copie
   de production modifiée à la main, qui n'est pas sur `main`, dont la base des comptes n'est pas
   dans `C:\MarginMate\data`, ou que git ne lit pas (« dubious ownership » : la fenêtre donne la
   commande `git config --global --add safe.directory …` à taper).
3. **Cherche les changements** enregistrés dans le dossier de développement (`git fetch origin`) et
   les liste. S'il n'y en a pas, il dit « Rien de nouveau » et s'arrête sans toucher au serveur -
   sauf si rien n'écoute sur le port 8765 : il dit alors que le site est hors ligne, et comment
   le relancer.
4. **Demande** « Déployer ces changements ? (O/N) ».
5. **Vérifie que rien ne tourne** (`manage.py running_jobs`) : une récupération de factures, un
   import de tickets ou un import des ventes en cours serait coupé net. Si quelque chose tourne, il
   le nomme et s'arrête : attendez la fin, puis relancez `deploy.cmd`. Il vérifie aussi que la tâche
   `MarginMate` lance bien `C:\MarginMate\app\start_production.cmd` (section 7).
6. **Arrête le serveur** : la tâche `MarginMate`, puis ce qui écoute encore sur le port 8765. Le
   site affiche une erreur Cloudflare jusqu'à la relance, en général une à deux minutes. Si un
   travail a commencé dans les secondes entre la vérification et l'arrêt, il vient d'être coupé :
   la fenêtre le dit, le serveur est relancé tel qu'il était, et rien n'est mis en ligne. Le
   travail apparaît interrompu : relancez-le (ou reprenez-le) depuis sa page, puis relancez
   `deploy.cmd` quelques minutes plus tard.
7. **Sauvegarde** les données (`manage.py backup_data`) dans `C:\MarginMate\backups`. Si la
   sauvegarde échoue, il relance le serveur tel qu'il était et s'arrête : rien n'a changé.
8. **Met le code à jour** (`git merge --ff-only origin/main` : le code avance, rien n'est
   réécrit). Si cela échoue, il remet le code d'avant et relance le serveur.
9. **Installe les dépendances** (`pip install -r requirements.txt`), **applique les migrations**
   (`manage.py migrate_tenants`, juste après la sauvegarde) et **vérifie** le serveur
   (`manage.py serve --verifier`). Si l'une de ces étapes échoue, il **ne relance pas** le serveur :
   le nouveau code et les données ne vont peut-être plus ensemble. La fenêtre affiche alors les
   commandes pour revenir en arrière (section 10.4), avec les bons numéros, et **la marque reste** :
   le `deploy.cmd` suivant refuse et redit ces commandes, au lieu de dire « Rien de nouveau ».
10. **Relance le serveur** (la tâche `MarginMate`, ou une fenêtre `start_production.cmd` s'il n'y
    a pas de tâche), attend qu'il écoute et affiche « Déployé : ancien..nouveau », les deux numéros
    de version.

La fenêtre reste ouverte à la fin : lisez-la avant de la fermer. À savoir :

- `deploy.cmd` fait partie du code qu'il met à jour : il travaille depuis une copie de lui-même,
  dans le dossier temporaire de Windows.
- Si le serveur tournait dans une fenêtre ouverte à la main plutôt que par la tâche, cette fenêtre
  affiche « Le serveur ne tourne pas » une fois arrêtée : c'est l'ancien serveur, fermez-la.
- Si la tâche `MarginMate` s'exécute « avec les autorisations maximales », lancez `deploy.cmd` en
  administrateur (clic droit).
- Le serveur recopie à chaque démarrage les fichiers du dossier `static` (styles et scripts des
  pages), quelle que soit leur date : rien d'autre à faire pour eux.

### 10.4 Revenir en arrière

Juste après un échec, `deploy.cmd` affiche lui-même ces commandes. Une mise en ligne restée à
moitié (échec après la mise à jour du code, fenêtre fermée en cours de route) laisse sa marque,
le dossier `C:\MarginMate\app\.git\marginmate-deploy` : tant qu'il est là, `deploy.cmd` refuse,
affiche ce qu'il y a noté (la version d'avant, la sauvegarde, l'étape) et les mêmes commandes.
Revenez à la version d'avant, relancez le serveur, puis effacez la marque, **seulement si aucune
autre fenêtre `deploy.cmd` n'est ouverte** :

```
rmdir /s /q C:\MarginMate\app\.git\marginmate-deploy
```

et relancez `deploy.cmd`. Pour revenir plus tard sur une version mise en ligne :

1. Arrêtez le serveur : Ctrl+C dans sa fenêtre (celle que la tâche `MarginMate` a ouverte).
2. Remettez le code d'avant :

   ```
   cd /d C:\MarginMate\app
   git reset --hard <version d'avant>
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   La version d'avant est le premier numéro de « Déployé : ancien..nouveau » ;
   `git log --oneline` les liste toutes.
3. Si la version retirée apportait des migrations, le code d'avant ne connaît pas les nouvelles
   colonnes : remettez aussi les données de la sauvegarde faite avant la mise en ligne. **Tout ce
   qui a été saisi depuis est alors perdu.**

   ```
   move C:\MarginMate\data C:\MarginMate\data.avant-retour
   robocopy "C:\MarginMate\backups\<date>\data" C:\MarginMate\data /E
   ```

4. Relancez le serveur : `schtasks /run /tn MarginMate`, ou double-cliquez sur
   `start_production.cmd`.

Le prochain `deploy.cmd` proposera de nouveau les mêmes changements, tant que le dossier de
développement les a : corrigez-les d'abord là-bas, dans un nouveau commit.

### 10.5 Rafraîchir les données de développement

`refresh_dev_data.cmd`, d'un double-clic dans le dossier de développement, remplace `data-dev` par
une copie d'une sauvegarde de la production :

- il refuse de tourner dans la copie de production (son `.env` dit `MARGINMATE_HTTPS=1`), et tant
  que quelque chose écoute sur le port 8000 : arrêtez d'abord `runserver` ou l'aperçu ;
- il refuse aussi quand le `.env` de développement pointe vers les données du site, n'a pas de
  dossier de données à lui, ou garde une base des comptes hors de `data-dev`
  (`MARGINMATE_ACCOUNTS_DB` doit être `…\data-dev\accounts.sqlite3` : sinon `runserver` écrirait
  dans les comptes du site, et `migrate_tenants` les migrerait) ;
- il prend la sauvegarde terminée la plus récente de `C:\MarginMate\backups` (un `manifest.json` et
  un dossier `data` : une sauvegarde coupée est passée), ou celle qu'on lui donne
  (`refresh_dev_data.cmd "C:\MarginMate\backups\2026-10-01_101500"`), jamais une `-INCOMPLET` ;
- il renomme le dossier actuel `data-dev.ancien-<date>` : **rien n'est effacé**, supprimez
  vous-même ces anciens dossiers quand ils ne servent plus ;
- il recopie le dossier `data` de la sauvegarde à sa place (robocopy). Le `.env` de la sauvegarde
  n'est jamais recopié : le dossier de développement garde le sien, sans identifiants.

Pour une copie toute fraîche, faites d'abord une sauvegarde en production (section 8). Si le code de
développement a des migrations que la production n'a pas encore, lancez ensuite, dans le dossier de
développement : `.venv\Scripts\python.exe manage.py migrate_tenants`.

## 11. Quand le serveur refuse de démarrer

| Message | À faire |
|---|---|
| `DJANGO_SECRET_KEY n'est pas définie` ou `la clé secrète …` (au chargement), `[accounts.E006]` | Une clé d'au moins 50 caractères aléatoires dans `.env` (section 5). |
| `[accounts.E007]` mode debug et hôte public | `DJANGO_DEBUG=False` dans `.env`. |
| `[accounts.E008]` aucun hôte public, ou « * » | `DJANGO_ALLOWED_HOSTS=gestion.<votre-domaine>,localhost,127.0.0.1`. |
| `[accounts.E009]`, `[security.W012]`, `[security.W016]`, `[security.W004]` | `MARGINMATE_HTTPS=1` dans `.env`. |
| `[accounts.E010]` adresse des liens de signature | `MARGINMATE_SITE_URL=https://gestion.<votre-domaine>`. |
| `[accounts.E011]` données dans le dossier du code | `MARGINMATE_TENANTS_ROOT` et `MARGINMATE_ACCOUNTS_DB` vers `C:\MarginMate\data`, à côté de `C:\MarginMate\app` (section 5). |
| `[staff.W001]` phrase de passe | `MARGINMATE_SIGNING_PASSPHRASE` dans `.env` (celle d'avant : sans elle, les clés de signature ne s'ouvrent plus). |
| `… migration(s) à appliquer`, `base introuvable` | Sauvegarde, puis `manage.py migrate_tenants`. Pour une base introuvable, restaurez-la depuis une sauvegarde, ou fermez l'espace : `migrate_tenants` et `deploy.cmd` passent alors sur lui (« fermé, sans base - ignoré »). |
| `Le port 8765 est déjà pris` | Un autre serveur tourne encore sur ce port (une autre fenêtre `start_production.cmd`, ou un `runserver` lancé sur ce port) : arrêtez-le. |

Quelques problèmes se voient seulement dans le navigateur :

- **Erreur Cloudflare 1033 ou 502.** Le tunnel tourne mais le serveur non. Lancez
  `start_production.cmd`, puis vérifiez que l'URL du tunnel est bien `http://127.0.0.1:8765`.
- **« MarginMate n'est pas en service pour le moment » (503).** Le tunnel est arrivé sur un serveur
  qui n'est pas `start_production.cmd` (un `runserver` ?), qui l'a refusé. Vérifiez que l'URL du
  tunnel est `http://127.0.0.1:8765`, arrêtez ce serveur et lancez `start_production.cmd`.
- **« Demande incorrecte » (400) sur toutes les pages.** Le nom public manque dans
  `DJANGO_ALLOWED_HOSTS`.

## Limites connues

- **Taille des envois.** Cloudflare, dans son offre gratuite, refuse les envois de plus de 100 Mo,
  avec sa propre page en anglais. Importez une grosse archive « Données » (jusqu'à 4 Go) depuis le
  PC, à l'adresse `http://127.0.0.1:8765`. Un dossier de tickets de plus de 100 Mo passe en
  plusieurs fois.
- **La déconnexion efface les brouillons.** « Se déconnecter » efface les brouillons que le site
  gardait dans le navigateur : un inventaire commencé et jamais enregistré est perdu, comme une
  reprise de consignes pas encore enregistrée. Vos préférences restent, comme les sources cochées
  pour « Récupérer les nouvelles factures » et l'ordre de tri des tableaux. Changer la clé secrète
  fait oublier au navigateur les brouillons et ces préférences, en plus de déconnecter tout le
  monde.
- **Trop de tentatives.** Après trop de mots de passe faux, la page de connexion demande de
  patienter un quart d'heure. Un inconnu qui essaie des mots de passe sur votre adresse depuis
  ailleurs ne bloque pas le navigateur où vous vous êtes déjà connecté, ni le PC du bar
  (`http://127.0.0.1:8765`) : sur un autre appareil, patientez un quart d'heure.
- **L'adresse des visiteurs.** Le serveur lit l'adresse du visiteur dans ce que le tunnel lui
  transmet. Après la mise en ligne, regardez le fichier de preuve de la première fiche signée par
  un salarié depuis son téléphone : l'adresse IP notée doit être celle de son téléphone (ou de sa
  box), pas une adresse de Cloudflare. Si c'est une adresse de Cloudflare, signalez-le : un réglage
  du serveur (`trusted_proxy_count`) est alors à changer.
- **Un seul processus.** Le serveur est un seul processus Waitress, avec 8 fils. C'est voulu :
  l'espace de chaque requête et le compteur de tentatives de connexion vivent dans ce processus.
- **Pas de journal des visites.** Seules les erreurs et les refus sont notés.
