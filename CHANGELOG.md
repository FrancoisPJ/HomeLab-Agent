# Journal des modifications

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) ; versions
[SemVer](https://semver.org/lang/fr/).

## [Non publié]

### Ajouté
- **Recherche Internet pour le modèle profond** : outils `web_search` (SearXNG auto-hébergé, API
  JSON) et `web_fetch` (texte lisible d'une page, taille et durée limitées). Nouveau profil Compose
  `web` (service `searxng`, aucun port publié, `deploy/searxng/settings.yml`), section `web_tools`
  (désactivée par défaut), secret `SEARXNG_SECRET` généré par `make init`. `web_fetch` n'atteint que
  des adresses publiques : `http(s)` seulement, refus de localhost, du réseau local, des métadonnées
  et des IPv6 locales, connexion à l'adresse vérifiée (pas de DNS rebinding), redirections
  revérifiées. Résultats marqués « données non fiables ».
- **Catalogue des services Home Assistant** (`GET /api/services`, rechargé à chaque reconnexion) :
  le modèle maison voit les services et champs principaux des `context_domains`
  (`home_assistant.services_max_chars`).
- **Photos et fichiers sur Telegram** : réponse claire (« Je ne peux pas encore lire les photos ni
  les fichiers… ») au lieu du silence ; une légende est traitée comme une demande, avec une note
  pour le modèle. Métrique `telegram_attachments_total`.

### Modifié
- **Séparation stricte des outils par niveau** (section `tools`, par étiquettes) : le modèle maison
  (GPU) garde Home Assistant, caméras, routines, mémoire et escalade ; le modèle profond (CPU) et le
  cloud ont Internet et la lecture de la mémoire, **sans aucun outil ni état Home Assistant** (ni la
  liste des entités dans le prompt). Un appel à un outil non proposé au niveau est refusé (`denied`
  dans l'audit). Après des échecs d'outils répétés, la main ne passe plus qu'à un niveau autorisé à
  utiliser ces outils. Ancien comportement du cloud : `tools.cloud: [ha, action, memory, web]`.
- Prompt système selon le niveau (consignes maison, ou « aucun accès à la maison » et consignes web).

### Corrigé
- **Services inventés** (`light.dim`, `light.turn_off` avec `brightness_pct`) : refusés avant
  l'appel avec la liste des services et champs possibles (`home_assistant.service_validation`) ; un
  refus HTTP 4xx de Home Assistant renvoie son message avec la même liste.
- Le petit modèle s'arrêtait après une erreur d'outil en expliquant la correction : il est relancé
  une fois pour rappeler l'outil corrigé.

## [0.5.1] - 2026-11-05

### Sécurité
- Clé de signature du cookie du panneau dérivée du mot de passe par PBKDF2-HMAC-SHA256
  (200 000 itérations) au lieu d'un simple SHA-256 : les sessions ouvertes sont déconnectées une
  fois après la mise à jour.
- `check-config` nomme le réglage concerné (`web.password_env`…) au lieu du nom de la variable.
- Faux LLM : marqueurs `[[tool:…]]` lus en un seul passage (plus de regex à retour arrière).

### Modifié
- `Services` déplacé dans `homelab_agent/services.py` (réexporté par `app.py`) : plus d'imports
  circulaires entre le démarrage, les commandes et le panneau. Alertes CodeQL : 46 → 3 (faux
  positifs).

### Corrigé
- **Chemin rapide trop permissif** : « je veux la lumière de la chambre allumée à 1 % » allumait la
  chambre sans régler la luminosité (le détail était ignoré). Le chemin rapide n'agit plus que si le
  message se réduit au verbe, à l'entité et à des mots de liaison ; sinon le LLM s'en charge.

## [0.5.0] - 2026-11-05

### Ajouté
- **Photos des caméras dans Telegram** : `/camera [pièce]` (boutons par pièce sans argument) et
  en langage naturel (« montre-moi l'entrée », outil `ha_camera_snapshot`). Désactivé par défaut
  (`home_assistant.cameras.enabled`) ; caméra soumise aux permissions de lecture, image envoyée
  seulement à la conversation qui la demande et jamais à un modèle, limite par minute, journal
  d'audit. Voir [docs/home-assistant.md](docs/home-assistant.md#caméras).
- **Supervision Prometheus + Grafana** : profil Compose `monitoring`, tableau de bord de 19
  panneaux provisionné automatiquement (niveaux, latences p50/p95, disponibilité des moteurs,
  outils, alertes, budget, voix), généré par `scripts/grafana_dashboard.py` et vérifié en CI ;
  `make dev-monitoring` pour la démo. Voir [docs/monitoring.md](docs/monitoring.md).
- `/metrics` complet : histogrammes de latence, jauges (moteurs, Home Assistant, tâches, budget),
  `HELP` / `TYPE` ; jeton de collecte `METRICS_TOKEN` limité à `GET /metrics`.
- **Panneau « liquid glass »** : surfaces en verre dépoli sur fond d'aurore animée, reflet qui suit
  le pointeur, réfraction réelle de la barre de navigation (navigateurs Chromium), bulle d'onglet qui
  glisse d'une page à l'autre (transitions de vue), thèmes auto / clair / sombre, respect de
  « réduire les animations » et « réduire la transparence ».
- Page d'accueil : 24 dernières heures en un coup d'œil (messages, latence p95, dépense, avec
  courbes), « Qui répond ? » (anneau par niveau), maison en direct (lumières, température, serrures,
  alertes en cours), jauge de budget.
- **Palette de commandes** (Ctrl K / ⌘ K, ou 🔍) : aller à une page, changer de thème, ou
  pré-remplir le chat (jamais envoyé sans Entrée). Sur téléphone, barre d'onglets en bas et
  pages secondaires dans la palette.
- `web.grafana_url` : lien Grafana dans la barre du panneau.
- Le **chat du panneau comprend les mêmes commandes que Telegram** (`/status`, `/routines`,
  `/memoire`, `/annuler`, `/help`…) : une seule table de commandes pour les deux passerelles.

### Modifié
- **Qualité du code** (sans changement de comportement) : le panneau web est découpé en un module
  par page (`web/routes/`), les fonctions longues de l'orchestrateur, de l'action Home Assistant,
  du flux de streaming et du démarrage sont découpées en étapes nommées et testées ; complexité
  plafonnée par ruff (`C90`, 13 au plus) ; couverture des tests de 87 % à 91 %.
- Une valeur invalide envoyée au panneau (politique de permission, action de routine) renvoie une
  erreur 400/422 au lieu d'une erreur serveur 500 ; le limiteur de connexions oublie les anciens
  essais au lieu de grossir indéfiniment.
- `make init` génère `PANEL_PASSWORD`, `METRICS_TOKEN` et `GRAFANA_ADMIN_PASSWORD` au hasard dans le
  nouveau `.env` (jamais les valeurs déjà saisies) et le passe en `chmod 600`.
- CI : vérifie aussi le JSON Grafana et le fichier Compose avec tous les profils.

## [0.4.0] - 2026-11-05

### Ajouté
- **Réponses vocales** (Piper, local) : note vocale OGG/Opus en plus du texte (`voice.reply_voice`),
  image `tts`, texte rendu prononçable ; aller-retour vérifié avec les vrais moteurs.
- **Annuler la dernière action** : bouton ↩️ sous les commandes rapides, `/annuler`, « annule » ;
  passe par les permissions.
- **Chat dans le panneau web** : deuxième passerelle (même agent, mêmes permissions), réponses en
  streaming (SSE), confirmations Oui/Non, résultats des tâches CPU reçus plus tard.
- **Mémoire longue** : « souviens-toi que… », notes annoncées, `/memoire`, `/oublie`, injectées
  comme données ; une routine ne peut pas en écrire.
- **Télécommande `/maison`** dans Telegram : pièces → appareils avec leur état, un appui bascule,
  sans LLM, permissions et confirmations appliquées.
- **Domotique proactive** : alertes sur changements d'état Home Assistant (conditions, durée,
  anti-répétition, heures calmes, bouton d'action), routines planifiées (vérification sans LLM,
  demande à l'agent, rappel simple, ponctuel ou répété), page « Proactif » du panneau,
  commandes `/alerts`, `/routines`, `/run`, `/routine`.
- **Routines en langage naturel** : « rappelle-moi de sortir les poubelles tous les mardis à 20h »,
  toujours confirmées par Oui/Non.
- **Messages vocaux** transcrits en local (whisper.cpp), profil Compose `voice`.
- **Modèle GPU « qualité »** pour les demandes moyennes, avec retour au modèle rapide si la VRAM
  manque ; préréglage `cpu-only` (`make init PRESET=cpu-only`).
- Budget de contexte (historique et résumé Home Assistant ajustés à `max_context`).
- Rechargement automatique de `config.yaml` modifié à la main, purge quotidienne de la base,
  commande `homelab-agent healthcheck`.
- Projet GitHub : modèles de tickets et de PR, CONTRIBUTING, SECURITY, devcontainer, CodeQL,
  CI sur toutes les branches, captures d'écran dans le README.

### Sécurité
- Services d'administration Home Assistant bloqués (`hassio.*`, `homeassistant.restart`…),
  cible explicite obligatoire pour les services d'entités.
- Une conversation ne peut plus écrire `${…}` dans la config (fuite de secret possible).
- Seul l'auteur d'une demande peut la confirmer ; limitation des essais de connexion au panneau ;
  masquage des secrets dans tous les formats de journaux.
- Empreintes de llama-swap et commits de llama.cpp / whisper.cpp vérifiés ; mises à jour de
  sécurité des images de base.

### Corrigé
- Anti-répétition des alertes conservé au redémarrage ; rappels ponctuels manqués rattrapés.
- Routines DST-safe ; chemin rapide non « capturable » par un nom d'entité.

## [0.1.0] - 2026-11-05

Première version publique.

- Agent Telegram (long polling, liste blanche, streaming, confirmations).
- Routage en entonnoir : chemin rapide sans LLM, GPU (llama.cpp CUDA 12.2 via llama-swap), CPU
  asynchrone persistant, cloud (Claude / Gemini) plafonné en euros, repli automatique.
- Home Assistant : client REST/WebSocket, outils génériques, moteur de permissions, audit.
- Panneau web (FastAPI + HTMX) : état, permissions, budget, tâches, audit, éditeur de config.
- Faux services (LLM, Home Assistant, Telegram), tests, scénario e2e, CI/CD.

[Non publié]: https://github.com/FrancoisPJ/HomeLab-Agent/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/FrancoisPJ/HomeLab-Agent/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/FrancoisPJ/HomeLab-Agent/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/FrancoisPJ/HomeLab-Agent/compare/v0.1.0...v0.4.0
[0.1.0]: https://github.com/FrancoisPJ/HomeLab-Agent/releases/tag/v0.1.0
