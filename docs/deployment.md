# Guide de déploiement

Le projet fonctionne de la même façon dans un **nouveau conteneur LXC dédié** ou dans une **VM
existante reconstruite** : il suffit de Docker. Pour le GPU dans un LXC, le passthrough NVIDIA doit
être configuré côté Proxmox (hors du périmètre de ce dépôt).

## 1. Préparer l'hôte

```bash
docker --version && docker compose version     # Compose v2.24+
nvidia-smi                                     # profil full uniquement
docker run --rm --gpus all nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi
```

## 2. Installer

```bash
git clone https://github.com/FrancoisPJ/HomeLab-Agent.git && cd HomeLab-Agent
make init
```

Éditez `.env` (tokens, `PANEL_PASSWORD`, `HLA_UID`/`HLA_GID` si votre uid n'est pas 1000) et
`config.yaml` (`allowed_user_ids`, `home_assistant.url`, modèles, routage). Puis :

```bash
make check
make models          # ~17 Go avec les modèles proposés
make up              # ou make up PROFILE=cpu-only
make ps && ./scripts/healthcheck.sh
```

Les commandes `make check`, `make engines` et `make models` utilisent [uv](https://docs.astral.sh/uv/)
sur l'hôte (`curl -LsSf https://astral.sh/uv/install.sh | sh`, puis `uv sync`). Sans uv,
`scripts/download-models.sh` se rabat sur l'image `homelab-agent:local` (`docker compose build agent`).

## 3. Mettre à jour

```bash
git pull && make restart
```

Images pré-construites : la CD publie `ghcr.io/<propriétaire>/homelab-agent-agent:edge` (et
`llama-cpu`, `llama-gpu`) ; remplacez `build:` par ces `image:` si vous ne voulez pas compiler.
Attention : l'image GPU publiée est compilée pour `CUDA_ARCHITECTURES=86` et CUDA 12.2.

## 4. Sauvegarder

`config.yaml`, `.env` et `data/agent.db` suffisent. Les modèles se re-téléchargent.

## Checklist de recette sur le vrai matériel

Ces points **n'ont pas pu être testés** pendant le développement (aucun accès au matériel). À cocher
par vous :

- [ ] `make up` : les trois conteneurs sont `healthy` (`make ps`).
- [ ] **GPU détecté** : `docker compose logs llama-gpu` montre le chargement sur CUDA à la première
      requête ; `nvidia-smi` montre le processus.
- [ ] **VRAM partagée avec Jellyfin** : lancez un transcodage, envoyez une demande ; le modèle se
      charge ou, sinon, la demande bascule sur le CPU avec un message ; après `ttl_s`, la VRAM est
      libérée.
- [ ] **Latence « éteins la lumière »** : avec un alias (chemin rapide) < 1 s ; via le modèle GPU
      chargé, quelques secondes au plus. Notez les valeurs.
- [ ] `scripts/bench.py` sur `fast`, `fallback` et `heavy` : jetons/s et score d'appel d'outils notés.
- [ ] **Job CPU long** : `/deep rédige un résumé de …` → accusé immédiat puis réponse ; redémarrez
      l'agent pendant le job (`docker compose restart agent`) : la réponse arrive quand même.
- [ ] **Confirmation Telegram** : activez la confirmation sur `lock` dans le panneau, demandez de
      déverrouiller une serrure de test : boutons Oui/Non, « Non » n'agit pas, « Oui » agit.
- [ ] **Permissions** : passez une entité en `deny` dans le panneau ; la demande suivante est refusée
      sans redémarrage ; l'audit montre `denied`.
- [ ] **Allowlist** : un autre compte Telegram n'obtient aucune réponse.
- [ ] **Plafond cloud** : avec une clé API, `cloud.enabled: true`, `daily_eur: 0.01` : après une ou
      deux requêtes `/cloud`, alerte puis repli local.
- [ ] Le panneau n'est pas joignable depuis une autre machine du réseau (seulement via `127.0.0.1` /
      tunnel SSH).
