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
- Profil `cpu-only` : le niveau GPU est normalement 🔴, tout bascule sur le CPU.

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
