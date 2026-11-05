# GPU : CUDA 12.2 et partage avec Jellyfin

## Image CUDA compatible avec le driver 535

Le driver NVIDIA 535 supporte CUDA **12.2** au maximum (`nvidia-smi` affiche « CUDA Version »). L'image
`llama-gpu` est donc compilée sur `nvidia/cuda:12.2.2-devel-ubuntu22.04` et s'exécute sur
`nvidia/cuda:12.2.2-runtime-ubuntu22.04`. Aucun driver plus récent n'est exigé.

Paramètres (dans `.env`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `CUDA_VERSION` | `12.2.2` | base de l'image ; doit être ≤ la version CUDA du driver |
| `CUDA_ARCHITECTURES` | `86` | architecture compilée (86 = RTX 30xx ; plusieurs : `"75;86;89"`) |

**Après une mise à jour du driver** : regardez la version CUDA dans `nvidia-smi`, choisissez une image
`nvidia/cuda:<version>-devel-ubuntu22.04` existante qui ne la dépasse pas (par exemple `12.4.1`),
mettez `CUDA_VERSION=12.4.1` dans `.env`, puis `docker compose --profile full build llama-gpu`.

La version de llama.cpp est épinglée (`LLAMA_CPP_REF` dans `docker/llama-gpu.Dockerfile`). Une version
récente de llama.cpp peut un jour abandonner CUDA 12.2 : si la compilation échoue, épinglez une
version plus ancienne.

Prérequis hôte : NVIDIA Container Toolkit installé et configuré pour Docker
(`nvidia-ctk runtime configure --runtime=docker`). Test : `docker run --rm --gpus all
nvidia/cuda:12.2.2-base-ubuntu22.04 nvidia-smi`.

## Partager la VRAM avec Jellyfin

La RTX 3070 a 8 Go ; un transcodage Jellyfin en occupe une partie, il reste environ 3 à 5 Go.

**Choix retenu : [llama-swap](https://github.com/mostlygeek/llama-swap)** en proxy devant
`llama-server`, dans le même conteneur. C'est l'option la plus simple qui couvre le besoin :

- un modèle n'est **chargé qu'à la première requête** qui le demande (champ `model` de l'API) ;
- il est **déchargé après `ttl_s` secondes d'inactivité** (`models.items[].ttl_s`), libérant la VRAM
  pour Jellyfin ;
- un seul modèle GPU à la fois : demander le modèle de secours décharge le rapide, et inversement ;
- la configuration de llama-swap est **générée** depuis `config.yaml` (`make engines`) : une seule
  source de vérité.

Si la VRAM est prise (Jellyfin transcode) et que le chargement échoue, llama-swap renvoie une erreur ;
l'agent marque le moteur indisponible pendant `routing.unhealthy_cooldown_s` et **bascule sur le
niveau CPU** en prévenant brièvement l'utilisateur.

### Modèle « qualité » : une montée en gamme, pas un secours

`routing.gpu_quality` désigne un second modèle GPU (≈ 8B) utilisé pour les demandes « moyennes »
(message d'au moins `min_chars` caractères, ou contenant un des `keywords`). Ce n'est **pas** un
modèle de secours : quand la VRAM manque pour le 4B, elle manque aussi pour le 8B. Fonctionnement :

1. demande moyenne → l'agent demande le modèle `quality` ; llama-swap décharge le modèle rapide et
   tente de charger le 8B ;
2. si le chargement échoue (Jellyfin occupe la VRAM), l'agent repasse **immédiatement sur le modèle
   rapide**, sans basculer sur le CPU, et laisse le modèle qualité de côté pendant `retry_after_s` ;
3. si le modèle rapide lui-même est indisponible, on bascule sur le niveau voisin (CPU), jamais sur
   le modèle qualité.

Le signal « VRAM libre » est donc le succès du chargement lui-même : pas besoin d'accès à
`nvidia-smi` depuis l'agent. Pour désactiver : `routing.gpu_quality.backend: null`.

Réglages utiles pour réduire l'empreinte : `cache_type_k/v: q8_0`, `flash_attn: true`, `ctx_size`
plus petit, ou `n_gpu_layers` inférieur à 99 (une partie du modèle reste en RAM, plus lent).

Le premier message après un déchargement attend le chargement du modèle (quelques secondes pour un
4B). Choisissez `ttl_s` selon votre usage : plus long = plus réactif, plus court = plus de VRAM pour
Jellyfin.

Alternative non retenue : un superviseur maison qui arrête le conteneur quand Jellyfin transcode
(plus complexe, nécessite l'accès à Docker depuis l'agent).

## Vérifier sur le vrai matériel

```bash
nvidia-smi                                   # VRAM avant
docker compose exec llama-gpu curl -s localhost:8080/running   # modèles chargés (llama-swap)
# envoyez « éteins la lumière » … puis :
nvidia-smi                                   # VRAM pendant
# attendez ttl_s secondes :
nvidia-smi                                   # VRAM libérée
```
