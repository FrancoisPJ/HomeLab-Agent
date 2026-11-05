# Dépannage

## Sécurité : ne jamais exposer llama-server

L'API de `llama-server` / llama-swap **n'a pas d'authentification par défaut**. Les services
`llama-gpu` et `llama-cpu` n'ont donc **aucun port publié** : ils ne sont joignables que depuis le
réseau Compose interne. Ne les publiez pas sur `0.0.0.0` et n'ouvrez pas leur port sur la box. Pour un
benchmark depuis l'hôte, publiez temporairement sur `127.0.0.1:8080` uniquement.

## Le bot ne répond pas

- `make logs` : cherchez `ignored message from non-allowlisted user <id>` → ajoutez cet identifiant
  numérique à `telegram.allowed_user_ids`.
- `Telegram is enabled but TELEGRAM_BOT_TOKEN is not set` → `.env`.
- Un seul processus peut faire du long polling avec un même token : arrêtez les autres instances.

## « Aucun moteur disponible »

`/status` ou la page d'accueil du panneau montrent quels moteurs sont 🔴.

- `llama-gpu` ne démarre pas : `docker compose logs llama-gpu`. Vérifiez le NVIDIA Container Toolkit
  et que `engines/llama-swap-gpu.yaml` existe (`make engines`).
- Fichier de modèle absent : `make models`, et `models.dir`/`MODELS_DIR` cohérents.
- Profil `cpu-only` : partez du préréglage (`make init PRESET=cpu-only`), qui sert le niveau rapide
  depuis `llama-cpu`. Avec la config par défaut, le niveau rapide est 🔴 et tout devient une tâche CPU.

### Vulnérabilité connue de llama-swap

Les versions v260 à v269 de llama-swap embarquent `golang.org/x/net` 0.59, concernée par une
faille HTTP/2 corrigée en 0.60 (signalée par trivy). Risque accepté pour l'instant : Go ne sert
le HTTP/2 que sur TLS, et llama-swap n'écoute que sur le réseau Compose interne, sans port publié.
À mettre à jour (`LLAMA_SWAP_VERSION` et `LLAMA_SWAP_SHA256` dans les Dockerfile) dès qu'une version
corrigée sort.

## « Message trop long pour gpu_fast »

Le message ne tient pas dans le contexte du modèle rapide (`max_context`) : il est envoyé au niveau
suivant. Pour des messages longs fréquents, augmentez `ctx_size` du modèle et `max_context` du backend
(plus de VRAM), ou laissez faire le CPU.

## « … is a blocked service » / « needs explicit entity_id targets »

Protection voulue : voir « Services bloqués » et « Cible obligatoire » dans
[home-assistant.md](home-assistant.md). Débloquez un service précis avec `allowed_services`.

## « Trop d'essais » sur le panneau

Après 5 mots de passe faux en 5 minutes depuis la même adresse, la connexion est refusée pendant
5 minutes.

## Le panneau refuse de démarrer

`Refusing to start the web panel: set PANEL_PASSWORD` : définissez `PANEL_PASSWORD` (8 caractères
minimum) dans `.env`, ou `web.enabled: false`.

## Le panneau ne peut pas enregistrer la configuration

Le conteneur écrit `config.yaml` avec l'utilisateur `HLA_UID:HLA_GID` (1000:1000 par défaut). Si votre
utilisateur hôte a un autre identifiant : `HLA_UID=$(id -u)` et `HLA_GID=$(id -g)` dans `.env`. Même
chose pour `./data`.

## Home Assistant 🔴

- `401` : token invalide ou expiré.
- Certificat auto-signé en HTTPS : `home_assistant.verify_ssl: false` (à éviter si possible).
- Le résumé d'entités est vide : vérifiez les permissions (`default_policy: deny` masque tout).

## Le cloud n'est jamais utilisé

`cloud.enabled: false` ou plafonds à 0 (défaut volontaire), clé API absente, ou `model` encore à
`<à renseigner>`. Voir [cloud-budget.md](cloud-budget.md).

## Les jobs CPU sont lents

C'est attendu : un 14B en Q4 sur CPU produit quelques jetons par seconde. Mesurez avec
`scripts/bench.py`, réduisez `max_output_tokens`, ou choisissez un modèle plus petit.

## Compilation de l'image GPU en échec

Voir [gpu-sharing.md](gpu-sharing.md) : version de CUDA, `CUDA_ARCHITECTURES`, version de llama.cpp.

## Réinitialiser

`make down`, puis supprimez `./data/agent.db` (historique, jobs, dépenses et audit sont perdus).
