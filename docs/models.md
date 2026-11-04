# Choisir les modèles

Le choix se fait dans `config.yaml`, section `models.items`, puis :

```bash
make engines    # génère engines/llama-swap-gpu.yaml et llama-swap-cpu.yaml
make models     # télécharge les GGUF manquants et vérifie leur SHA256
make restart
```

## Méthode

1. **Budget mémoire.** Taille du fichier GGUF + cache KV (contexte) + ~0,5 Go de marge.
   - GPU rapide : la RTX 3070 a 8 Go, mais Jellyfin peut en prendre pendant un transcodage : visez
     **≤ 3 Go** pour le modèle rapide.
   - GPU secours : ≈ 5 Go, utilisé seulement quand la VRAM est libre (llama-swap décharge le modèle
     rapide avant de le charger ; si le chargement échoue, on bascule sur le CPU).
   - CPU lourd : ≈ **12 Go** disponibles → un modèle dense de 14B en Q4 (≈ 9 Go) passe ; un MoE de 30B
     (≈ 18 Go) ne passe pas.
2. **Quantification.** `Q4_K_M` est le compromis courant ; `Q5_K_M` si la mémoire le permet.
   `cache_type_k/v: q8_0` + `flash_attn: true` réduisent le cache KV sur GPU.
3. **Appel d'outils.** Le petit modèle doit produire des appels de fonction fiables : c'est le critère
   principal. `llama-server` est lancé avec `--jinja` pour utiliser le gabarit de chat du modèle (requis
   pour les outils).
4. **Mesurer.** Sur le vrai matériel :
   ```bash
   docker compose exec llama-gpu curl -s localhost:8080/v1/models   # vérifier que le moteur répond
   uv run python scripts/bench.py --url http://127.0.0.1:8080/v1 --model fast --runs 3
   ```
   (publiez temporairement le port 8080 du moteur sur `127.0.0.1` ou lancez le script dans le réseau
   Compose). Le script mesure le délai avant le premier jeton, les jetons/s et le score sur six cas
   d'appel d'outils domotiques. Gardez le plus petit modèle qui obtient 6/6 avec une latence acceptable.

### Mesure de référence (pas sur votre matériel)

Pendant le développement, la chaîne réelle image `llama-cpu` → llama-swap → llama-server → agent a
été vérifiée avec un modèle minuscule (`Qwen/Qwen3-0.6B-GGUF`, `Qwen3-0.6B-Q8_0.gguf`) sur 4 vCPU de
cloud : environ 59 jetons/s en décodage, et **3 à 4 sur 6** au test d'appel d'outils (il a par exemple
répondu `turn_on` à « éteins »). Conclusion : sous ~3B, les appels d'outils ne sont pas fiables, d'où
le chemin rapide sans LLM pour les commandes simples. Ces chiffres ne disent rien de la RTX 3070 ni de
l'i9-10850K : mesurez chez vous.

## Candidats (à vérifier au moment de l'installation)

Les noms et fichiers ci-dessous ont été vérifiés sur Hugging Face lors de l'écriture de ce document
(dépôts officiels Qwen, licence Apache 2.0 indiquée sur la fiche). Les modèles évoluent vite :
re-vérifiez la licence et le fichier, et préférez le SHA256 affiché sur Hugging Face.

| Usage | Dépôt | Fichier | Taille | SHA256 (Hugging Face) |
|---|---|---|---|---|
| GPU rapide (défaut) | `Qwen/Qwen3-4B-GGUF` | `Qwen3-4B-Q4_K_M.gguf` | ≈ 2,5 Go | `7485fe6f…fdf5` |
| GPU secours (défaut) | `Qwen/Qwen3-8B-GGUF` | `Qwen3-8B-Q4_K_M.gguf` | ≈ 5,0 Go | `d98cdcbd…5785` |
| CPU lourd (défaut) | `Qwen/Qwen3-14B-GGUF` | `Qwen3-14B-Q4_K_M.gguf` | ≈ 9,0 Go | `500a8806…81f0` |

Autres familles à évaluer avec `bench.py` (dépôts existants, fichiers à choisir vous-même) :

| Famille | Dépôts repérés | Remarque |
|---|---|---|
| Qwen 2.5 Instruct | `Qwen/Qwen2.5-3B-Instruct-GGUF`, `Qwen/Qwen2.5-7B-Instruct-GGUF`, `Qwen/Qwen2.5-14B-Instruct-GGUF` | la fiche du 3B indique une licence « other » : à lire ; certains fichiers sont découpés en plusieurs parties (non géré par `download-models.sh`, prenez un fichier unique) |
| Llama 3.2 | `bartowski/Llama-3.2-3B-Instruct-GGUF` | quantifications communautaires, licence Llama à accepter |
| Petits modèles d'appel d'outils | TODO | non vérifiés : ne pas ajouter de dépôt sans l'avoir confirmé |

**Piste MoE** : si la RAM disponible dépasse ~20 Go, un MoE comme `Qwen/Qwen3-30B-A3B-GGUF` (dépôt
existant) donne souvent une meilleure qualité qu'un 14B dense à vitesse CPU comparable, car seule une
fraction des paramètres est active par jeton. Hors budget avec 12 Go.

**Qwen3 et le « thinking »** : ces modèles peuvent produire un raisonnement avant la réponse. Pour le
niveau rapide, ajoutez `/no_think` dans `agent.system_prompt` si les réponses sont trop lentes, et
comparez avec `bench.py`.

## Ajouter un modèle

```yaml
models:
  items:
    - name: rapide2
      engine: gpu
      repo: <dépôt Hugging Face>
      file: <fichier>.gguf
      sha256: <empreinte affichée sur Hugging Face>
      ctx_size: 4096
      flash_attn: true
      ttl_s: 600
backends:
  gpu_fast:
    model: rapide2     # le backend pointe vers le nouveau nom
```
