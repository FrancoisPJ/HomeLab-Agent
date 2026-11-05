# Journal des modifications

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) ; versions
[SemVer](https://semver.org/lang/fr/).

## [Non publié]

### Ajouté
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
  CI sur toutes les branches.

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

[Non publié]: https://github.com/FrancoisPJ/HomeLab-Agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/FrancoisPJ/HomeLab-Agent/releases/tag/v0.1.0
