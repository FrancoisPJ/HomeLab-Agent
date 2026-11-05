# Journal des modifications

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) ; versions
[SemVer](https://semver.org/lang/fr/).

## [Non publié]

### Ajouté
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

[Non publié]: https://github.com/FrancoisPJ/HomeLab-Agent/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/FrancoisPJ/HomeLab-Agent/compare/v0.1.0...v0.4.0
[0.1.0]: https://github.com/FrancoisPJ/HomeLab-Agent/releases/tag/v0.1.0
