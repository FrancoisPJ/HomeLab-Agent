# Architecture

## Vue d'ensemble

Un seul conteneur applicatif, `agent`, regroupe la gateway Telegram, l'orchestrateur, le routeur, la
file de jobs et le panneau web. Les moteurs d'inférence sont des conteneurs séparés qui exposent tous
l'API compatible OpenAI (`/v1/chat/completions`) : le code ne dépend d'aucun service propriétaire
(pas d'Ollama).

```
                    ┌──────────────────────── conteneur agent ─────────────────────────┐
Telegram ◄─polling─►│ gateway ─► orchestrateur ─► routeur ──► clients LLM              │──► llama-gpu (llama-swap → llama-server CUDA)
                    │              │   ▲            │         (openai_compat,          │──► llama-cpu (llama-swap → llama-server CPU)
                    │              │   │            └─► budget  anthropic, gemini)     │──► API Claude / API Gemini
                    │              ▼   │                                               │
                    │        registre d'outils ─► permissions ─► client HA (REST + WS) │──► Home Assistant :8123
                    │              │                    └─► audit                      │
                    │     file de jobs CPU (SQLite)       panneau web (FastAPI+HTMX)   │◄── 127.0.0.1:8099
                    └──────────────────────────────────────────────────────────────────┘
```

## Modules (`src/homelab_agent/`)

| Module | Rôle |
|---|---|
| `config/` | modèles pydantic, chargement YAML, `${VAR}`, erreurs lisibles, écriture avec conservation des commentaires |
| `gateway/base.py` | interfaces `Gateway`, `Replier`, `Notifier` (prêtes pour WhatsApp / voix en phase 2) |
| `gateway/telegram.py` | allowlist, commandes, streaming par édition, boutons Oui/Non — indépendant de la bibliothèque |
| `gateway/aiogram_adapter.py` | branchement sur l'API Bot via aiogram 3 en long polling |
| `gateway/home_remote.py` | télécommande `/maison` (claviers Telegram, sans LLM) |
| `web/chat.py` | deuxième passerelle : chat du panneau (SSE), boîte de réception, aiguillage des notifications |
| `core/memory.py` | mémoire longue (notes par conversation, injectées comme données) |
| `core/orchestrator.py` | boucle agent : message → chemin rapide ou routage → LLM ↔ outils → réponse |
| `core/fastpath.py` | commandes triviales sans LLM |
| `core/router.py` | choix du niveau, santé des moteurs, repli sur le niveau voisin |
| `core/jobs.py` | file persistante des tâches CPU, reprise après redémarrage |
| `core/budget.py` | dépenses cloud, plafonds jour/mois, alertes |
| `core/proactive.py` | alertes sur changements d'état Home Assistant, routines planifiées |
| `llm/` | interface commune (chat, outils, streaming, jetons) + clients llama-server, Claude, Gemini |
| `tools/registry.py` | outils déclarés en JSON Schema, validation des arguments, outil interne `escalate` |
| `tools/homeassistant/` | client REST/WS + cache, moteur de permissions, outils exposés au LLM |
| `store/` | SQLite (SQLAlchemy 2 async) : historique, interactions, jobs, dépenses, audit ; migrations simples |
| `web/` | panneau : état (graphiques SVG générés côté serveur, `charts.py`, `summary.py`), permissions, budget, jobs, audit, éditeur de config ; `static/ui.js` = petites améliorations facultatives (thème, palette Ctrl K, compteurs) |
| `engines.py` | génère la configuration llama-swap depuis `models:` |
| `voice/stt.py` | client de transcription (API `/v1/audio/transcriptions`, servie par whisper.cpp) |
| `fakes/` | faux LLM, faux Home Assistant, fausse API Telegram, faux STT (tests, CI, profil `dev`) |
| `observability/` | journaux JSON avec masquage des secrets, métriques Prometheus (`/metrics`) |

## Le routage, étape par étape

1. **Chemin rapide** (`routing.fast_path`) : si le message est court, sans question, contient un seul
   verbe connu (allume, éteins, ouvre, ferme, active…) et désigne sans ambiguïté une entité (alias de la
   config, ou nom Home Assistant porté par **une seule** entité), l'action est appelée directement —
   **en passant par les permissions**. Les alias passent avant les noms : une entité renommée ne peut
   pas « capter » un alias.
2. **Décision de niveau** : `/cloud` et `/deep` forcent le niveau ; sinon mots-clés cloud, mots-clés
   CPU, longueur du message ; par défaut GPU.
3. **Résolution** : premier moteur du niveau qui est en bonne santé (et, pour le cloud, dans le
   budget). Sinon on essaie les niveaux voisins dans `routing.fallback_order` et l'utilisateur reçoit
   une ligne d'explication (« GPU rapide : gpu_fast indisponible → bascule sur CPU lourd »).
4. **GPU** : le petit modèle reçoit le prompt système, le résumé des entités, l'historique et les
   outils. Il répond ou appelle des outils (boucle limitée par `agent.max_tool_rounds`). Il peut
   appeler `escalate(target, reason)`. Après `max_tool_failures` échecs d'outils, on passe la main.
5. **CPU** : la demande devient un job SQLite ; accusé immédiat, réponse envoyée à la fin. Un job à la
   fois par défaut. Les jobs `running` repassent en `queued` au redémarrage.
6. **Cloud** : synchrone, streamé ; coût calculé avec les tarifs de la config et comptabilisé.
7. **Erreurs** : moteur injoignable / 5xx / délai → marqué indisponible pendant
   `unhealthy_cooldown_s`, et on rejoue la demande sur le niveau voisin.

Chaque réponse enregistre niveau, moteur, modèle, latence, jetons et coût (table `interactions`,
visible sur la page d'accueil du panneau).

## Source de vérité de la configuration

**Décision : `config.yaml` est l'unique source de vérité.** Le panneau web écrit dans ce fichier
(en conservant les commentaires grâce à ruamel.yaml), après validation complète, puis recharge la
configuration en mémoire. Il n'y a pas de surcouche en base. Avantages : un seul endroit à lire et à
sauvegarder, diff lisible, pas de divergence. Limite : les changements de moteurs, de Telegram ou du
panneau demandent un redémarrage (les permissions, le routage et le budget sont pris à chaud).

## Sécurité, en bref

- Rien n'écoute publiquement ; les moteurs llama.cpp n'ont pas d'authentification et restent sur le
  réseau Compose.
- Secrets uniquement par variables d'environnement, masqués dans les journaux.
- Permissions Home Assistant appliquées dans le code, au même point pour le chemin rapide, le LLM
  local et le cloud.
- Les textes venant de Home Assistant sont nettoyés et présentés comme **données** ; les cibles d'une
  action ne peuvent pas être glissées dans `data`.
- Conteneurs non-root, système de fichiers en lecture seule, `no-new-privileges`, `cap_drop: ALL`.
