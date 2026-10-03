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
données de **`C:\MarginMate\data`**. Le code se modifie ailleurs, dans un dossier de développement,
s'envoie sur GitHub (branche `main`), et se met en ligne par `deploy.cmd` : section 10.

## Avant de commencer

- **Le site vit sur ce PC.** Quand le PC est éteint ou en veille, le site ne répond plus. Dans
  Paramètres > Système > Alimentation, réglez la mise en veille sur « Jamais » (sur secteur).
- **Deux serveurs, deux ports.** Le serveur de production écoute sur `http://127.0.0.1:8765` :
  c'est l'adresse que vise le tunnel. `runserver`, le serveur de développement, garde son port
  habituel, 8000 : il n'est pas fait pour Internet et le tunnel ne le vise jamais.
- **Chaque copie a son `.env`.** Les sections 5 à 9, 11 et 12 parlent de la copie de production,
  `C:\MarginMate\app`, et de son `.env`. Chaque dossier de développement a le sien (section 10).
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

`MARGINMATE_SITE_URL` doit rester l'adresse https du site : c'est elle qui active les
notifications sur les téléphones et les récupérations automatiques (section 13).

Ces lignes sont déjà dans votre `.env`. Gardez-les telles quelles :

- `DJANGO_SECRET_KEY` : au moins 50 caractères tirés au hasard. Le serveur refuse de démarrer
  avec une clé trop courte, celle de l'exemple ou une clé « django-insecure ». Pour en générer une :
  `.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`.
  En changer déconnecte tout le monde, annule les codes de signature en cours et rend illisibles les
  identifiants tapés sur la page « Identifiants » : il faut alors les retaper (section 12).
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

Les identifiants des comptes (la boîte mail des factures, Metro, L'Addition, les espaces clients)
se tapent sur la page « Identifiants » du site. Une fois qu'ils y sont, leurs lignes n'ont plus rien
à faire dans ce fichier : section 12.

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
   puis la fenêtre affiche « Rappels et récupérations automatiques : actifs. » et
   `En ligne sur http://127.0.0.1:8765/`. Laissez-la ouverte.
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
  mots de passe pas encore tapés sur la page « Identifiants » (section 12).

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

Quatre choses ne sont **jamais** dans une sauvegarde, exprès, et la commande le dit :

- les mots de passe tapés sur la page « Identifiants » : à retaper après une restauration
  (section 12) ;
- les sessions de connexion : une session ouvre le site à qui la détient. Elles sont retirées de
  chaque base copiée, y compris des copies de base rangées dans `C:\MarginMate\data` (une copie
  faite à la main comme `accounts.sqlite3.bak_…`, celle gardée d'avant l'adoption, les copies de
  sécurité de la page « Données »). Après une restauration, chacun se reconnecte ;
- les pages que gardent les récupérations en échec et les « Tester » (les dossiers `_debug`) : elles
  peuvent montrer un identifiant ;
- une copie du fichier `.env` rangée dans `C:\MarginMate\data` (un fichier `.env` ou `.env.…`,
  comme `.env.bak_…`) : elle contient la clé secrète et des mots de passe. La commande la nomme,
  dans une ligne « ATTENTION », sans jamais l'ouvrir : supprimez-la (section 12). Le vrai `.env`,
  celui de `C:\MarginMate\app`, est copié à côté de `data\`, comme avant.

**Une fois, après la mise en ligne de cette version.** Les sauvegardes faites avant elle, et les
dossiers `data-dev.ancien-<date>` mis de côté avant elle, gardent encore des sessions de connexion
du site (et des pages `_debug`). Changez une fois votre mot de passe MarginMate : un nouveau mot de
passe ferme toutes les sessions ouvertes avec l'ancien, celles copiées dans ces dossiers
comprises. Dans une invite de commandes :

```
cd /d C:\MarginMate\app
.venv\Scripts\python.exe manage.py changepassword --database accounts <votre adresse e-mail>
```

La commande demande deux fois le nouveau mot de passe. Faites de même pour chaque autre compte de
vos espaces, avec son adresse, et donnez-lui son nouveau mot de passe : chacun se reconnecte
ensuite. Puis supprimez ces anciennes sauvegardes et ces dossiers `data-dev.ancien-<date>` dès
qu'ils ne servent plus, ou acceptez qu'ils gardent ces anciennes sessions, désormais fermées, et ces
pages. La copie du `.env` rangée dans le dossier des données, les anciennes pages `_debug` et les
mots de passe encore dans le `.env` demandent eux aussi un passage, une fois : section 12, « Une
fois, après la mise en ligne de cette version ».

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

Retapez ensuite les mots de passe sur la page « Identifiants » (section 12).

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

MarginMate existe en **plusieurs copies** sur ce PC, chacune avec son code, son `.env` et ses données :
la copie de production, et un ou plusieurs dossiers de développement.

| | Production : le site en ligne | Développement : les modifications |
|---|---|---|
| Code | `C:\MarginMate\app` | `C:\Users\<vous>\Desktop\Bar application gestion\AdminMate` (ou dans `Bar application gestion 2`, `… 3`) |
| Données | `C:\MarginMate\data` | le dossier `data-dev` à côté de `AdminMate`, une copie |
| Sauvegardes | `C:\MarginMate\backups` | aucune : ses données sont une copie |
| Serveur | `start_production.cmd` (la tâche `MarginMate`), port 8765, celui du tunnel | `runserver`, `http://localhost:8000` |
| `.env` | `DJANGO_DEBUG=False`, `MARGINMATE_HTTPS=1` (section 5) | `DJANGO_DEBUG=True`, sans `MARGINMATE_HTTPS`, sans identifiants |

- **On ne modifie jamais `C:\MarginMate\app` à la main.** Il ne change que par `deploy.cmd`, qui y
  apporte la branche `main` **de GitHub** (`https://github.com/paulbaron/MarginMate`) : un travail
  part en ligne une fois enregistré (`git commit`), fusionné dans `main` et envoyé (`git push`). Un
  fichier modifié mais pas enregistré, ou un commit pas envoyé, ne part pas.
- **GitHub est la seule source** depuis le 01/10/2026, quel que soit le dossier de développement
  qui a envoyé le code : il faut donc Internet pour mettre en ligne.
- **Le dépôt GitHub est public** : avant chaque envoi, vérifiez qu'aucun commit ne contient de
  vraies données (factures, banque, personnel, noms et montants de `data-dev`).
- **Une session Claude Code s'ouvre toujours dans le dossier de développement**, jamais dans
  `C:\MarginMate` : elle y modifie le code, lance les tests et, pour un aperçu, `runserver` sur
  `data-dev`.
- **`data-dev` contient les vraies données du bar**, copiées : factures, banque, personnel. Elles
  ne vont jamais dans git, ni dans un test, ni dans un exemple.

### 10.1 Mise en place (une seule fois)

Jusqu'ici le site tournait depuis le dossier de développement, sur
`C:\Users\<vous>\Desktop\Bar application gestion\data`. Pour passer aux deux copies :

1. Dans le dossier de développement, enregistrez tout le travail en cours sur la branche `main`
   (`git status` doit dire qu'il ne reste rien à enregistrer), puis envoyez-le sur GitHub :
   `git push origin main`. La copie de production ne recevra que ce qui y est.
2. Arrêtez le serveur : Ctrl+C dans la fenêtre de `start_production.cmd` (et `runserver`, s'il
   tourne).
3. Dans une invite de commandes, créez la copie de production du code et son Python (mise doit
   déjà être installé : section 10.6, étape 1) :

   ```
   mkdir C:\MarginMate
   git clone -b main https://github.com/paulbaron/MarginMate.git C:\MarginMate\app
   cd /d C:\MarginMate\app
   mise install
   uv sync --locked --no-dev --python 3.11
   ```

   `uv` crée le dossier `.venv`, avec Python 3.11, et y installe les dépendances : exactement
   celles du fichier `uv.lock`, sans les outils de développement.

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
   (leurs noms sont dans Factures > Sources). Sans eux, une récupération lancée depuis la copie
   refuse ; avec eux, elle se connecterait pour de vrai à Metro et aux portails. Donnez aussi à la
   copie sa propre `DJANGO_SECRET_KEY` (la section 5 dit comment en tirer une) : avec celle du site,
   `refresh_dev_data.cmd` refuse. Gardez `MARGINMATE_SIGNING_PASSPHRASE` : les clés de signature
   copiées en ont besoin pour s'ouvrir.
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
3. **Enregistrer** : `git add` puis `git commit`, sur `main` (ou sur une branche, puis fusionnée
   dans `main`).
4. **Envoyer sur GitHub** : `git push origin main`. Seul ce qui y est part en ligne. Si git refuse
   l'envoi (« rejected »), un autre dossier de développement a envoyé entre-temps : récupérez son
   travail (`git pull --no-rebase origin main`), relancez les tests, puis renvoyez. Jamais
   `git push --force`.
5. **Mettre en ligne** : double-cliquez sur `C:\MarginMate\app\deploy.cmd`.

### 10.3 Ce que fait `deploy.cmd`

Il travaille dans la copie de production, dans cet ordre, chaque étape seulement si la précédente
a réussi :

1. **Une seule mise en ligne à la fois** : il pose une marque, le dossier
   `C:\MarginMate\app\.git\marginmate-deploy`, et refuse de démarrer si elle est déjà là (une autre
   fenêtre `deploy.cmd` ouverte, ou une mise en ligne restée à moitié : section 10.4). Il y note où
   il en est (`etat.txt`) et l'efface en finissant. Avant de la poser, il vérifie que `uv` répond :
   sinon il refuse, sans rien toucher (section 10.6).
2. **Refuse de tourner ailleurs qu'en production** : un `.env` qui dit `DJANGO_DEBUG=True`, ou qui
   ne dit pas `MARGINMATE_HTTPS=1`, est celui du dossier de développement. Il refuse aussi une copie
   de production modifiée à la main, qui n'est pas sur `main`, dont la base des comptes n'est pas
   dans `C:\MarginMate\data`, dont le `.env` désigne le dossier des données par un autre chemin
   que le vrai (une jonction, un lien, un lecteur substitué, un nom court comme `MARGIN~1` : la
   fenêtre donne le vrai), ou que git ne lit pas (« dubious ownership » : la fenêtre donne la
   commande `git config --global --add safe.directory …` à taper).
3. **Cherche les changements** envoyés sur GitHub (`git fetch origin` : il faut Internet) et les
   liste ; sa fenêtre parle encore du « dossier de developpement » : c'est GitHub qu'elle lit.
   S'il n'y en a pas, il dit « Rien de nouveau » et s'arrête sans toucher au serveur -
   sauf si rien n'écoute sur le port 8765 : il dit alors que le site est hors ligne, et comment
   le relancer. « Rien de nouveau » après un `git commit` : le travail n'a pas été envoyé sur
   GitHub (section 10.2, étape 4).
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
9. **Installe les dépendances** (`uv sync --locked --no-dev --python 3.11` : exactement celles de
   `uv.lock`, sans les outils de développement, dans le `.venv` existant), **applique les migrations**
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
   uv sync --locked --no-dev --python 3.11
   ```

   Gardez `--python 3.11` : sans lui, une version d'avant le 01/10/2026 fait demander à `uv` un
   autre Python exact, et `uv` refait alors `.venv` en entier.
   La version d'avant est le premier numéro de « Déployé : ancien..nouveau » ;
   `git log --oneline` les liste toutes. On ne revient pas avant le passage à uv (section 10.6) :
   une version d'avant n'a pas de fichier `uv.lock`, et son `deploy.cmd`, remis en place par le
   retour, ne saurait plus mettre en ligne la suivante.
3. Si la version retirée apportait des migrations, le code d'avant ne connaît pas les nouvelles
   colonnes : remettez aussi les données de la sauvegarde faite avant la mise en ligne. **Tout ce
   qui a été saisi depuis est alors perdu.**

   ```
   move C:\MarginMate\data C:\MarginMate\data.avant-retour
   robocopy "C:\MarginMate\backups\<date>\data" C:\MarginMate\data /E
   ```

4. Relancez le serveur : `schtasks /run /tn MarginMate`, ou double-cliquez sur
   `start_production.cmd`.

Le prochain `deploy.cmd` proposera de nouveau les mêmes changements, tant que `main` sur GitHub
les a : corrigez-les d'abord dans un dossier de développement, dans un nouveau commit envoyé sur
GitHub.

### 10.5 Rafraîchir les données de développement

`refresh_dev_data.cmd`, d'un double-clic dans le dossier de développement, remplace `data-dev` par
une copie d'une sauvegarde de la production :

- il refuse de tourner dans la copie de production (son `.env` dit `MARGINMATE_HTTPS=1`), et tant
  que quelque chose écoute sur le port 8000 : arrêtez d'abord `runserver` ou l'aperçu ;
- il refuse aussi quand le `.env` de développement pointe vers les données du site, n'a pas de
  dossier de données à lui, ou garde une base des comptes hors de `data-dev`
  (`MARGINMATE_ACCOUNTS_DB` doit être `…\data-dev\accounts.sqlite3` : sinon `runserver` écrirait
  dans les comptes du site, et `migrate_tenants` les migrerait). Il regarde ce que chaque chemin est
  vraiment : une jonction, un lien, un lecteur substitué ou un nom court (`MARGIN~1`) qui mène à
  `C:\MarginMate\data` est refusé comme `C:\MarginMate\data` lui-même, et le `.env` de
  développement doit désigner `data-dev` par son vrai chemin (la fenêtre le donne) ;
- il refuse une sauvegarde faite avec la même `DJANGO_SECRET_KEY` que le `.env` de développement :
  la copie doit avoir la sienne (section 10.1, étape 9), sinon elle ouvrirait les sessions du site
  et ce qui est scellé avec cette clé ;
- il refuse tant que `data-dev`, ou les données de la sauvegarde, contiennent une copie du fichier
  `.env` (un fichier `.env` ou `.env.…`, comme `.env.bak_…`) : la fenêtre la nomme, sans l'ouvrir.
  Supprimez-la (section 12, « Une fois, après la mise en ligne de cette version »), puis
  relancez-le. Il ne recopie de toute façon jamais un tel fichier ;
- il prend la sauvegarde terminée la plus récente de `C:\MarginMate\backups` (un `manifest.json` et
  un dossier `data` : une sauvegarde coupée est passée), ou celle qu'on lui donne
  (`refresh_dev_data.cmd "C:\MarginMate\backups\2026-10-01_101500"`), jamais une `-INCOMPLET` ;
- il renomme le dossier actuel `data-dev.ancien-<date>` : **rien n'est effacé**, supprimez
  vous-même ces anciens dossiers quand ils ne servent plus ;
- il recopie le dossier `data` de la sauvegarde à sa place (robocopy). Le `.env` de la sauvegarde
  n'est jamais recopié : le dossier de développement garde le sien, sans identifiants. Les mots de
  passe de la page « Identifiants » et les pages des récupérations en échec (dossiers `_debug`) ne
  le sont pas non plus, même d'une sauvegarde plus ancienne qui les contient encore ;
- il efface les sessions de connexion qu'une sauvegarde plus ancienne contient encore, dans chaque
  base de la copie (la base des comptes, une copie faite à la main comme `accounts.sqlite3.bak_…`,
  celle gardée d'avant l'adoption…) : ce sont celles du site. S'il n'y arrive pas, la fenêtre le
  dit (« ATTENTION ») : réglez le problème et relancez-le.

Le dossier `data-dev.ancien-<date>` mis de côté n'est ni effacé ni nettoyé : il garde les sessions
de connexion et les pages `_debug` de l'ancienne copie, et la fenêtre le rappelle à la fin.
Supprimez-le dès qu'il ne sert plus. Pour ceux mis de côté avant cette version : section 8, « Une
fois, après la mise en ligne de cette version ».

Pour une copie toute fraîche, faites d'abord une sauvegarde en production (section 8). Si le code de
développement a des migrations que la production n'a pas encore, lancez ensuite, dans le dossier de
développement : `.venv\Scripts\python.exe manage.py migrate_tenants`.

### 10.6 Passage à uv (une seule fois)

Les dépendances de MarginMate (Django, le lecteur de tickets…) s'installent désormais avec
**uv**, aux versions exactes du fichier `uv.lock`. uv lui-même est installé par **mise**, qui le
garde à la version que fixe le fichier `mise.toml` du code. La production y passe en deux mises en
ligne :

1. **Installez mise**, une fois pour tout le PC. Dans une invite de commandes :

   ```
   winget install jdx.mise
   ```

   Puis ouvrez **PowerShell** (menu Démarrer, tapez `powershell`) et collez-y cette ligne, qui met
   les raccourcis de mise (ses « shims ») dans le PATH :

   ```powershell
   [Environment]::SetEnvironmentVariable("Path", "$env:LOCALAPPDATA\mise\shims;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
   ```

   Si mise est déjà installé pour le dossier de développement (README.md), c'est déjà fait.
2. **La première mise en ligne** est celle de la version `05a80b4`, la dernière à porter le fichier
   `requirements.txt` : elle se fait comme d'habitude, et c'est encore le `deploy.cmd` d'avant qui
   la conduit (il travaille depuis une copie de lui-même), en installant les dépendances depuis ce
   fichier, avec pip. Le code ne l'a plus depuis : si la production est restée avant `05a80b4`
   (pas de fichier `mise.toml` dans `C:\MarginMate\app`), son `deploy.cmd` s'arrête sur « ECHEC
   pendant l'installation des dependances (pip install -r requirements.txt) », le code déjà mis à
   jour, les données sauvegardées et le serveur arrêté. Ne revenez pas en arrière : faites
   l'étape 3, puis finissez la mise en ligne à la main, dans la même invite de commandes :

   ```
   uv sync --locked --no-dev --python 3.11
   .venv\Scripts\python.exe manage.py migrate_tenants
   .venv\Scripts\python.exe manage.py serve --verifier
   rmdir /s /q .git\marginmate-deploy
   schtasks /run /tn MarginMate
   ```
3. **Avant la mise en ligne suivante**, ouvrez une **nouvelle** invite de commandes (une fenêtre
   ouverte avant l'étape 1 ne voit pas le nouveau PATH) et tapez :

   ```
   cd /d C:\MarginMate\app
   mise install
   uv --version
   ```

   `mise install` installe les outils aux versions de `mise.toml` (rien, s'ils sont déjà là), et
   `uv --version` doit répondre par un numéro de version, comme `uv 0.12.20`. Si mise demande
   s'il faut faire confiance (« trust ») au fichier `mise.toml` de ce dossier, répondez oui : c'est
   celui du code. Ces commandes ne marchent qu'après la première mise en ligne, qui apporte
   `mise.toml` dans `C:\MarginMate\app`.
4. **À partir de la deuxième mise en ligne**, `deploy.cmd` installe les dépendances avec
   `uv sync --locked --no-dev --python 3.11` : exactement celles de `uv.lock`, sans les outils de développement,
   et il retire de `.venv` ce que `uv.lock` ne liste pas (pip compris). Si `uv` ne répond pas, il
   refuse avant de toucher à quoi que ce soit, avec « REFUS : uv ne repond pas » : reprenez
   l'étape 3. Si `uv --version` répond dans une nouvelle invite de commandes et que `deploy.cmd`
   refuse encore, fermez la session Windows et rouvrez-la.
5. `requirements.txt` a disparu du code après `05a80b4` : il ne servait qu'à la première mise en
   ligne. On ne revient donc pas à une version d'avant le passage à uv (section 10.4).

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

## 12. Les identifiants des comptes et la protection du dossier

Les identifiants avec lesquels MarginMate va chercher les factures et les ventes (la boîte mail des
factures, Metro, L'Addition, les espaces clients des fournisseurs) se tapent sur la page
« Identifiants » du site, plutôt que dans le `.env`. On l'ouvre depuis l'en-tête de la page
« Données », ou depuis Achats > Sources. Seul le propriétaire de l'espace y a accès.

**Le site vous redemande votre mot de passe MarginMate** avant chacune de ces actions. Une fois
tapé, il vaut pour ce navigateur, jusqu'à un quart d'heure après la dernière d'entre elles :

- ouvrir la page « Identifiants », et y enregistrer ;
- enregistrer ou « Tester » une source « Espace client » (Achats > Sources) ;
- sur la page « Données », importer une archive et effacer (exporter ne le demande pas) ;
- l'administration du site (`/admin/`), toutes ses pages après celle de connexion.

Sur la page « Identifiants » elle-même :

- Un mot de passe n'est **jamais réaffiché** : laissé vide, son champ garde celui qui est
  enregistré, et « Effacer » le retire.
- Ils sont **chiffrés, sur ce PC**, dans le dossier `private` de l'espace, avec une clé que Windows
  réserve à ce PC et à ce compte Windows, et la `DJANGO_SECRET_KEY`. Ils ne vont ni dans la base, ni
  dans une archive « Données ».
- Ils **ne sont dans aucune sauvegarde** (section 8), ni dans la copie de développement (section
  10.5). Après une restauration, sur un autre PC, sous un autre compte Windows ou après un
  changement de `DJANGO_SECRET_KEY`, la page dit qu'ils ne peuvent pas être lus : retapez-les.
  Gardez-les donc aussi ailleurs, dans un gestionnaire de mots de passe par exemple.
- **Les espaces clients dont le mot de passe est encore dans le `.env`** ne le reçoivent plus
  d'office depuis cette version : sur la page, chacun dit « Fichier .env : site à confirmer ». Une
  fois, pour chacun, vérifiez que l'adresse indiquée est bien celle du site de ce compte, puis
  **au choix** : cochez sa case « Le fichier .env contient ces identifiants : les envoyer à … » (une
  case par espace client) et enregistrez, **ou** tapez sur la page son identifiant **et** son mot
  de passe, les deux (le mot de passe seul ne suffit pas), et enregistrez. Sinon, la récupération
  de cet espace client s'arrête, avec un message qui le dit. Une fois les deux tapés sur la page,
  retirez ses lignes du `.env` (ci-dessous).
- Votre navigateur peut proposer d'enregistrer les mots de passe tapés sur cette page :
  **refusez** (« Jamais » pour ce site). Ce sont ceux de vos fournisseurs et de votre boîte mail,
  pas celui de MarginMate : le navigateur en garderait une copie de plus, hors de MarginMate, et
  les proposerait sur d'autres pages.

### Une fois, après la mise en ligne de cette version

Trois choses à faire une seule fois, dans cet ordre, quand aucune récupération ne tourne.

**1. Une copie du `.env` rangée dans le dossier des données.** Une copie du `.env` de production
laissée dans `C:\MarginMate\data` (par exemple `C:\MarginMate\data\.env.bak_…`) part avec les
données dans chaque sauvegarde, puis dans `data-dev`, que lisent les sessions de
programmation : elle contient la clé secrète, la phrase de passe et des mots de passe.
`backup_data` ne copie plus aucun fichier `.env` ou `.env.…` du dossier des données et nomme
chacun (« ATTENTION ») ; `refresh_dev_data.cmd` refuse tant qu'il en trouve un dans `data-dev` ou
dans la sauvegarde qu'il prend. Cette commande les liste ; si elle ne trouve rien, passez au
point 2 :

```
dir /s /b /a-d C:\MarginMate\data\.env*
```

1. Ouvrez chaque copie et `C:\MarginMate\app\.env` dans le Bloc-notes, et comparez leurs lignes
   `DJANGO_SECRET_KEY`. **Si c'est la même clé**, changez celle de `C:\MarginMate\app\.env` (la
   section 5 dit comment en tirer une), puis relancez le serveur : tout le monde est déconnecté, et
   la page « Identifiants » redemande les identifiants, qu'elle ne peut plus lire. Retapez-les. Si
   la copie contient aussi des mots de passe encore valables (la boîte mail, Metro, L'Addition, un
   espace client), changez-les chez le fournisseur, puis sur la page.
2. Supprimez chaque copie du `.env` rangée dans `C:\MarginMate\data` : une fois fini, la commande
   ci-dessus ne doit plus rien trouver.

3. Supprimez ses copies dans les données de développement, `data-dev\.env.…` (le dossier
   `data-dev` est à côté du dossier de développement : section 10), et celles des dossiers
   `data-dev.ancien-<date>`.
4. Faites une nouvelle sauvegarde (section 8) : elle ne contient pas la copie. Puis supprimez les
   sauvegardes plus anciennes qui la contiennent (`C:\MarginMate\backups\<date>\data\.env.…`),
   leurs copies hors du PC comprises. Pour en garder une, supprimez-y au moins ce fichier.

**2. Les pages gardées par les récupérations avant cette version.** Les récupérations en échec et
les « Tester » gardaient les pages des espaces clients telles quelles, sans rien masquer : elles
peuvent montrer un identifiant. Celles gardées désormais sont masquées. Supprimez les anciennes :
dans les dossiers `downloads` de chaque espace, les dossiers `_debug` de chaque source et les
dossiers `test-…`. Dans PowerShell :

```powershell
Remove-Item C:\MarginMate\data\tenants\*\downloads\*\_debug -Recurse -Force
Remove-Item C:\MarginMate\data\tenants\*\downloads\test-* -Recurse -Force
```

Rien d'autre n'est effacé : les factures téléchargées restent. Les sauvegardes et les dossiers
`data-dev.ancien-<date>` faits avant cette version en gardent aussi (section 8, « Une fois, après la
mise en ligne de cette version »).

**3. Les identifiants encore dans le `.env`.** Pour chaque espace client, la case « Le fichier .env
contient ces identifiants : les envoyer à … », ou son identifiant et son mot de passe tapés sur la
page (ci-dessus) ; puis les lignes du `.env` à retirer (ci-dessous).

### Retirer les mots de passe du `.env`

Une fois un compte tapé sur la page, et la page indiquant « Enregistré ici » pour lui,
**supprimez du `.env` de production** (`C:\MarginMate\app\.env`) ses lignes : tant qu'elles y sont,
le mot de passe reste en clair dans ce fichier, et dans chaque sauvegarde, qui le copie. Les
récupérations lisent d'abord la page : rien ne change pour elles. Les lignes concernées :

- la boîte mail : `INVOICE_EMAIL_ADDRESS` et `INVOICE_EMAIL_APP_PASSWORD` (et `INVOICE_IMAP_HOST`,
  si elle y est). Un `.env` plus ancien peut les nommer `UBA_EMAIL_ADDRESS` et
  `UBA_EMAIL_APP_PASSWORD` : supprimez-les aussi ;
- Metro : `METRO_EMAIL` et `METRO_PASSWORD` ;
- L'Addition : `LADDITION_EMAIL` et `LADDITION_PASSWORD` ;
- chaque espace client : ses deux variables, dont les noms sont sur la fiche de sa source (Achats >
  Sources).

Le reste du `.env` reste en place : la clé secrète, la phrase de passe, les lignes de la section 5,
`ANTHROPIC_API_KEY` et le serveur d'e-mails (`EMAIL_HOST`…), que la page ne prend pas.

Puis relancez le serveur (Ctrl+C dans sa fenêtre, puis `schtasks /run /tn MarginMate`) : tant qu'il
n'a pas redémarré, la page continue de signaler ces mots de passe « encore en clair » dans le
`.env`. L'avertissement disparaît au redémarrage.

### Réserver `C:\MarginMate` à votre compte

Un dossier créé à la racine de `C:` est ouvert à tous les comptes du PC : chacun peut y lire les
bases, les fichiers et le `.env`, et même les modifier. Réservez-le à votre compte, à Windows
lui-même et aux administrateurs. Dans une invite de commandes **en administrateur** (section 3,
étape 4) :

```
icacls C:\MarginMate /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
```

Si Windows a demandé le mot de passe d'un autre compte pour passer administrateur, remplacez
`%USERNAME%` par le nom de votre compte. Pour vérifier :

```
icacls C:\MarginMate\data
```

ne doit lister que votre compte, `AUTORITE NT\Système` et `BUILTIN\Administrateurs`, chacun suivi
de `(I)` : tout ce que contient le dossier suit. Le serveur, la tâche `MarginMate`, `deploy.cmd` et
les sauvegardes tournent sous votre compte : rien ne change pour eux. Une copie sur un disque externe
n'emporte pas ces droits : rangez le disque à l'abri.

Ces droits ne protègent rien de ce qui tourne sous votre propre compte Windows. Aujourd'hui, tout
programme lancé dans votre session peut lire le `.env` de production et les identifiants de la page
« Identifiants » : Windows ouvre leur clé pour votre compte, et la `DJANGO_SECRET_KEY` est dans le
`.env`. C'est le cas de **Claude Code et des sessions de programmation** que vous ouvrez dans le
dossier de développement : seule leur consigne (ne jamais toucher à `C:\MarginMate`) les en tient
à l'écart, rien ne les en empêche.

**Mieux encore : un compte Windows réservé au serveur.** Un compte local standard, avec lequel
personne n'ouvre de session, à qui seul `C:\MarginMate` appartient et sous lequel tourne la tâche
`MarginMate` : ce qui s'ouvre dans votre session (une pièce jointe piégée, un programme installé,
Claude Code) ne peut plus lire les données ni le `.env`. C'est un changement à préparer : la tâche
ne montre plus de fenêtre (ses messages restent dans le journal, section 9), `deploy.cmd` se lance
sous ce compte et doit pouvoir lire le dossier de développement, et les identifiants, réservés au
compte qui fait tourner le serveur, sont à retaper une fois le changement fait.

## 13. Les notifications et les récupérations automatiques

Deux pages arrivent avec cette version : **Données › Notifications** (des rappels programmés et des
alertes, sur vos téléphones) et **Factures › Récupération automatique** (les bons du livreur et les
factures reçues par mail, relevés tout seuls aux heures choisies).

### Au premier déploiement

- `deploy.cmd` applique lui-même les nouvelles migrations (accounts 0003, notifications 0001,
  invoices 0036) : c'est son étape `manage.py migrate_tenants`, après sa sauvegarde. Rien à faire
  à la main en production.
- Le dossier de développement, lui, ne migre pas `data-dev` tout seul : `runserver` arrêté, lancez
  une fois `.venv\Scripts\python.exe manage.py migrate_tenants` **dans le dossier de
  développement** (jamais dans `C:\MarginMate`). Sans cela, Factures et Consignes ne s'affichent
  plus sur `data-dev`. Un rafraîchissement (section 10.5) ramène une copie de la production : une
  fois la production déployée, elle a déjà ces tables.
- `MARGINMATE_SITE_URL` doit rester l'adresse https du site (section 5). Le serveur n'envoie
  aucune notification et ne lance aucune récupération tout seul si elle manque, si elle n'est pas
  en https, si le mode debug est actif ou si la clé secrète est faible. C'est le cas du dossier de
  développement, et c'est voulu : il ne peut jamais écrire aux téléphones du site ni relever la
  boîte mail à sa place.

### Activer les notifications sur un téléphone

**iPhone et iPad** (iOS 16.4 ou plus) : les notifications passent par l'application.

1. Dans Safari, ouvrez le site, touchez Partager › **Sur l'écran d'accueil**, et laissez « Ouvrir
   comme app web » activé.
2. Ouvrez MarginMate **depuis la nouvelle icône**, pas depuis Safari, et reconnectez-vous :
   l'application ne partage pas la connexion de Safari.
3. Données › Notifications › « Activer les notifications sur cet appareil », puis acceptez.

**Android** : dans Chrome, ouvrez le site, puis Données › Notifications › « Activer les
notifications sur cet appareil », et acceptez.

« Envoyer un essai », sur la même page, vérifie que tout marche. Chaque personne active ses
propres appareils ; « Mes appareils » liste les siens. Une connexion dure deux semaines : passé ce
délai, toucher une notification ouvre d'abord la page de connexion, puis la page prévue. Se
déconnecter coupe les notifications de cet appareil jusqu'à la prochaine connexion.

### Le PC doit être allumé

Les rappels et les récupérations automatiques partent du PC du bar : à l'heure prévue, il doit
être allumé, le serveur lancé, et **pas en veille**. Sur secteur, réglez la mise en veille sur
« Jamais » (Paramètres › Système › Alimentation). Un rappel en retard de plus de 30 minutes n'est
plus envoyé : l'historique de la page Notifications dit « manqué ». Cette page dit aussi si le
planificateur tourne.

Une récupération automatique manquée est rattrapée dans le délai de « Toutes les », 2 h au plus,
ou dans les 12 h pour une récupération une fois par jour ; plus tard, elle dit « manquée ». Chaque
règle affiche son délai. Si une autre récupération est en cours à l'heure prévue, la règle dit
« en attente : une récupération est en cours » et part dès que l'autre est finie. Si la base est
occupée à cet instant, elle dit « en attente : base occupée » et réessaie à la minute suivante ;
passé le délai, « manquée : base occupée à HH:MM ».

Chaque source reprend là où sa dernière récupération réussie s'est arrêtée, 90 jours au plus :
après une panne de trois semaines, les trois semaines sont relevées. Le serveur retient, pour
chaque boîte mail de factures et chaque format de bons, jusqu'à quel jour elle a été relevée sans
trou. Seule une recherche menée jusqu'au bout compte, à la main ou automatique : une source en
échec, une récupération annulée ou interrompue ne compte pas. Ne compte pas non plus une
recherche où le serveur mail n'a pas tout rendu (la ligne dit « Recherche incomplète : N e-mail(s)
non lu(s) par le serveur mail. » ou « Recherche refusée par le serveur mail ») ni celle dont un
document n'a pas pu être importé faute de lecture ou de base disponible : ce qui a été lu est
importé quand même, la source compte comme en échec, et la récupération suivante reprend la même
période (les documents déjà importés sont reconnus et ignorés). Une facture en double ou un
fichier illisible, eux, n'empêchent pas la source d'avancer. Une récupération à la main qui part
après un trou (la date proposée par Achats, une période passée) ne le comble pas : la prochaine
récupération automatique le relève, y compris pour une source jamais relevée. Modifier
l'expéditeur, l'objet, le corps ou la pièce jointe recherchés d'une source de factures par mail,
ou les motifs d'un format de bons, la fait repartir de sa propre date de départ : les jours déjà
relevés l'avaient été pour d'autres mails. Changer seulement le nom ne change rien. Si ces
réglages sont enregistrés pendant qu'une récupération tourne, sa recherche ne compte pas pour
cette source (le journal dit « réglages de recherche modifiés pendant la récupération ») : la
récupération suivante repart de la nouvelle date de départ.

Au-delà de 90 jours, chaque récupération automatique redit sur la ligne de la source « Rattrapage
à faire à la main depuis Factures, du … au … », et l'alerte de fin la compte comme une source en
échec. « au » est le jour où la limite des 90 jours a coupé la période : il ne change pas d'un
jour à l'autre. La règle l'affiche aussi, et Achats propose cette date dans « Du » tant que la dernière
récupération à la main a relevé cette source sans erreur. « Récupérer » depuis cette date règle
le rattrapage ; une récupération qui s'arrête avant n'en règle qu'une partie, et la règle affiche
ce qui reste. Pour les bons, rien n'est cherché plus de 400 jours en arrière : un rattrapage plus
ancien se règle à partir de cette limite. Une récupération qui a rapporté des factures ou des bons
envoie l'alerte « Récupération automatique : du nouveau ».

### L'import automatique des ventes

Les ventes de la caisse (L'Addition) ont leur propre import automatique, séparé de la
récupération des factures et des bons : **Recettes & ventes › Ventes › « Import automatique :
réglages »** (aussi depuis Données › Notifications).

- Le prochain `deploy.cmd` applique aussi la migration **recipes 0018** (même étape
  `migrate_tenants`, après sa sauvegarde). Dans le dossier de développement, le même
  `migrate_tenants` à la main que ci-dessus l'applique à `data-dev`.
- Pour importer chaque matin les ventes de la veille : gardez la règle proposée « Ventes de la
  veille », tous les jours, à 07:00, et cliquez « Ajouter ». Rien ne part au moment
  d'enregistrer : le premier import part à la prochaine heure prévue.
- La période va de la dernière journée importée sans trou, moins 3 jours, jusqu'à la dernière
  nuit terminée : avant l'heure « La nuit se termine à » (Notifications › Rappels, 06:00 par
  défaut), la veille n'est pas encore finie et s'arrête l'avant-veille. Jamais plus de 400 jours en
  arrière. Les imports faits à la main depuis l'onglet Ventes comptent aussi.
- Quand les ventes sont déjà à jour, la règle dit « à jour : ventes importées jusqu'au JJ/MM » et
  ne se connecte pas à L'Addition : même avec plusieurs heures dans la journée, L'Addition reçoit
  environ une connexion par jour.
- Un seul import des ventes à la fois : si un import tourne déjà (à la main ou automatique), la
  règle attend (« en attente : un import des ventes est en cours ») et part dès qu'il est fini.
  Si cet import échoue ou est annulé, la règle ne recommence pas aussitôt (« sautée : l'import en
  cours vient d'échouer ou d'être annulé ») : elle part à sa prochaine heure. Un import manqué (PC
  éteint) est rattrapé dans les 12 h.
- Effacer les ventes dans « Données », ou un « Remplacer » qui supprime des journées de caisse,
  ramène l'import automatique avant la première journée supprimée : l'import suivant la reprend
  (le compte rendu le dit).
- L'alerte « Import automatique des ventes » (Notifications › Alertes) prévient d'un échec, une
  fois par jour au plus ; « ventes importées » et « rien de nouveau » se cochent si vous le voulez.

### Changer la clé secrète

Les notifications sont signées avec une clé tirée de `DJANGO_SECRET_KEY`. En changer oblige à
réactiver chaque téléphone : ouvrir l'application suffit en général (elle se réinscrit seule),
sinon Données › Notifications › « Activer ».

### Un déploiement refusé, « Données » occupé

Une récupération automatique en cours compte comme une récupération lancée à la main :
`deploy.cmd` refuse de démarrer, et « Données » demande d'attendre. Patientez quelques minutes, ou
décochez « Active » sur la récupération automatique (Factures › Récupération automatique) le temps
de l'opération. Un import automatique des ventes en cours fait de même (décochez « Actif » dans
Recettes & ventes › Ventes › Import automatique). Pendant un déploiement, les récupérations et les
imports prévus sont sautés (« sautée : mise à jour du site en cours »). Metro et les espaces clients ne sont jamais récupérés automatiquement :
ils restent à la main, depuis Factures.

### Vérifier après le déploiement

Dans une invite de commandes :

```
curl -sI https://gestion.<votre-domaine>/sw.js
curl -sI https://gestion.<votre-domaine>/manifest.webmanifest
```

Le premier doit répondre `content-type: text/javascript; charset=utf-8` et
`cache-control: no-cache`, sans `cf-cache-status: HIT`. Le second
`content-type: application/manifest+json`. Dans Cloudflare, aucune règle « Cache Everything » ni
défi (challenge, Bot Fight Mode) sur ces deux adresses : un téléphone qui ne reçoit pas le vrai
fichier ne reçoit plus de notifications.

## Limites connues

- **Taille des envois.** Cloudflare, dans son offre gratuite, refuse les envois de plus de 100 Mo,
  avec sa propre page en anglais. Importez une grosse archive « Données » (jusqu'à 4 Go) depuis le
  PC, à l'adresse `http://127.0.0.1:8765`. Un dossier de tickets de plus de 100 Mo passe en
  plusieurs fois. « Prendre une photo » (Factures, sur le téléphone) s'arrête avant 90 Mo : tant
  qu'une photo attend, ce qui ferait dépasser 90 Mo à l'envoi (une photo de plus, des fichiers
  choisis ensuite) est refusé sur la page, qui demande d'importer d'abord ce qui est déjà choisi.
  Les photos prises ne vont en général pas dans la galerie du téléphone : quitter la page avant
  « Importer » les perd, et le navigateur demande d'abord.
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
