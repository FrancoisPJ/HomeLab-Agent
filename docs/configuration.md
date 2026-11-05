# Configuration

Deux fichiers, créés par `make init` :

- **`.env`** : les secrets (tokens, clés API, mot de passe du panneau) et quelques réglages Compose.
  Jamais commité.
- **`config.yaml`** : tout le reste. Validé par pydantic ; le schéma JSON exporté est
  `config.schema.json` (la première ligne de `config.example.yaml` l'active dans VS Code).

```bash
make check      # valide config.yaml et signale les variables d'environnement manquantes
make schema     # régénère config.schema.json après une modification du code
```

Les valeurs peuvent référencer l'environnement : `url: ${HA_URL}` ou `${HA_URL:-http://homeassistant.local:8123}`.
Pour écrire littéralement `${...}`, doublez le dollar : `$${...}`. Par sécurité, un texte venant
d'une conversation (routine créée depuis Telegram) ne peut jamais contenir `${`.
Les champs `*_env` contiennent le **nom** d'une variable, jamais la valeur.

Une erreur donne un message lisible, par exemple :

```
invalid configuration:
  - web.port: Input should be less than or equal to 65535
  - routing.tiers.gpu references unknown backend 'gpu_rapide'
```

## Sections

### `agent`
| Clé | Défaut | Rôle |
|---|---|---|
| `language` | `fr` | langue des réponses |
| `timezone` | `Europe/Paris` | heure dans le prompt, bornes jour/mois du budget |
| `log_level` / `log_format` | `info` / `json` | journaux |
| `history_messages` | 12 | messages de contexte conservés |
| `max_tool_rounds` | 6 | aller-retours LLM ↔ outils par réponse |
| `system_prompt` | — | consignes ajoutées au prompt système |

### `telegram`
`token_env`, `allowed_user_ids` (identifiants numériques), `stream_edit_interval_s`,
`max_message_chars`, `confirm_timeout_s`, `api_base` (serveur Bot API alternatif). Voir [telegram.md](telegram.md).

### `backends`
Un dictionnaire `nom → moteur`. Champs communs : `tier` (`gpu`, `cpu`, `cloud`), `model`,
`timeout_s`, `max_output_tokens`, `temperature`.

| `kind` | Champs propres |
|---|---|
| `openai_compat` | `url` (avec `/v1`), `api_key_env` (facultatif), `max_context` (doit correspondre au `ctx_size` du modèle), `concurrency` (requêtes simultanées, à aligner sur `parallel`) |
| `anthropic` | (l'API Messages actuelle ignore `temperature`, qui n'est pas envoyée) |
| `anthropic` | `api_key_env`, `base_url` |
| `gemini` | `api_key_env` |

Pour les moteurs locaux, `model` est le `name` d'un élément de `models.items` (llama-swap s'en sert pour
charger le bon fichier).

**Budget de contexte.** Avant chaque appel à un moteur local, l'agent estime la taille du prompt
(prompt système, résumé Home Assistant, outils, historique, message) et la fait tenir dans
`max_context - max_output_tokens` : il retire d'abord les plus anciens échanges, puis raccourcit le
résumé Home Assistant. Si le message seul ne tient pas — ou si le serveur répond quand même « context
size exceeded » — la demande passe au moteur suivant (niveau voisin), sans marquer le moteur en panne.

### `routing`
| Clé | Rôle |
|---|---|
| `fast_path.enabled`, `fast_path.aliases` | chemin rapide et alias « nom prononcé → entity_id » |
| `fast_path.match_names` | reconnaître aussi les noms Home Assistant (seulement si une seule entité porte ce nom) ; `false` = alias uniquement |
| `gpu_quality.backend`, `min_chars`, `keywords`, `retry_after_s` | montée en qualité sur GPU pour les demandes moyennes ([gpu-sharing.md](gpu-sharing.md)) |
| `tiers.gpu / cpu / cloud` | moteurs de chaque niveau, par ordre de préférence (l'ordre du cloud = préférence Claude/Gemini) |
| `escalation.to_cpu_keywords`, `to_cloud_keywords` | mots-clés d'escalade |
| `escalation.long_message_chars` | au-delà, le message va au CPU (0 = jamais) |
| `escalation.max_tool_failures` | échecs d'outils avant de passer la main |
| `escalation.allow_model_escalation` | expose l'outil `escalate` au petit modèle |
| `fallback_order` | ordre des niveaux pour le repli |
| `health_interval_s`, `unhealthy_cooldown_s` | sondes de santé et quarantaine d'un moteur en échec |

### `jobs`
`max_concurrent` (1), `poll_interval_s`, `max_attempts`.

### `cloud`
`enabled`, `pricing.<backend>.in_per_mtok / out_per_mtok` (€ par million de jetons),
`limits.daily_eur`, `limits.monthly_eur`, `limits.alert_at`, `limits.per_provider`. Voir [cloud-budget.md](cloud-budget.md).

### `home_assistant`
`url`, `token_env`, `verify_ssl`, `default_policy`, `rules`, `confirm`, `blocked_services`,
`allowed_services`, `untargeted_domains`, `context_max_chars`, `context_domains`,
`request_timeout_s`, `services_max_chars` (1200 : catalogue des services montré au modèle, 0 =
masqué), `service_validation` (`fields` par défaut : service ou champ inconnu refusé avant l'appel ;
`services` : seulement les services ; `off`). Voir [home-assistant.md](home-assistant.md#catalogue-des-services).
Préférez une URL `https://` :
en HTTP, le token transite en clair sur le réseau local.
`cameras` (désactivé par défaut) : `enabled`, `model_tool`, `max_per_minute`, `max_per_request`,
`max_bytes`, `timeout_s` — photos des caméras envoyées dans Telegram, jamais à un modèle. Voir
[home-assistant.md](home-assistant.md#caméras).

### `web`
`enabled`, `bind` (127.0.0.1), `port`, `password_env`. Dans Docker, le conteneur écoute sur
`0.0.0.0` (variable `HLA_WEB_BIND`) mais le port n'est publié que sur `127.0.0.1` de l'hôte.
`metrics_token_env` (`METRICS_TOKEN` par défaut, `null` pour désactiver) nomme la variable du jeton
qui ouvre **uniquement** `GET /metrics` à Prometheus ; `grafana_url` (URL `http://` ou `https://`)
ajoute un lien « Grafana » à la barre du panneau. Voir [monitoring.md](monitoring.md).

### `tools`
Outils proposés à chaque niveau, par **étiquette** : un outil n'est proposé que si **toutes** ses
étiquettes sont listées pour ce niveau (un outil avec une étiquette nouvelle reste donc caché).

| Niveau | Défaut | Effet |
|---|---|---|
| `gpu` | `ha, action, camera, routines, routine_edit, memory, memory_edit, internal` | modèle maison : Home Assistant, caméras, routines, mémoire, escalade |
| `cpu` | `web, memory` | modèle profond (`/deep`) : Internet et lecture de la mémoire, **aucun accès à Home Assistant** |
| `cloud` | `web, memory` | comme `cpu` : un service tiers n'agit pas sur la maison |

Étiquettes : `ha` (lire Home Assistant), `action` (agir), `camera`, `routines` / `routine_edit`,
`memory` / `memory_edit`, `internal` (outil `escalate`), `web` (`web_search`, `web_fetch`). Sans
`ha`, le niveau ne reçoit pas non plus la liste des entités dans son prompt. Un appel à un outil qui
n'est pas proposé au niveau est refusé (`denied` dans l'audit), même si le modèle l'invente.
Conséquences : une demande sur la maison envoyée en `/deep` ou en `/cloud` ne peut pas agir, et les
routines `tier: cpu` ne lisent pas la maison. Pour revenir à l'ancien comportement du cloud :
`tools.cloud: [ha, action, memory, web]`. Voir [SECURITY.md](../SECURITY.md).

### `web_tools`
Recherche Internet pour les niveaux qui ont l'étiquette `web` (désactivé par défaut) :
`enabled`, `searxng_url` (`http://searxng:8080`, profil Compose `web`), `language`, `safesearch`,
`max_results` (5), `timeout_s` (15), `fetch_max_bytes` (2 Mo), `fetch_max_chars` (6000),
`max_redirects` (3), `blocked_domains` (globs). Voir [Recherche Internet](#recherche-internet-web_tools).

### `memory`
Mémoire longue : `enabled`, `max_items`, `context_max_chars`. Voir [telegram.md](telegram.md#mémoire-longue).

### `voice`
Messages vocaux Telegram : `enabled`, `stt_url`, `language`, `max_duration_s`, `echo_transcript`,
`timeout_s`, `stt_model`, `api_key_env` ; réponses vocales : `reply_voice`, `tts_url`, `tts_voice`,
`tts_speed`, `tts_max_chars`, `tts_timeout_s`. Voir [voice.md](voice.md).

### `proactive`
Alertes sur événements Home Assistant et routines planifiées (`enabled`, `alerts_enabled`,
`routines_enabled`, `notify_chat_ids`,
`quiet_hours`, `alerts`, `routines`, `action_ttl_s`, `chat_edit`, `max_routines`). Les routines
peuvent être créées depuis Telegram, après confirmation. Voir [proactive.md](proactive.md).

### `models`
Les GGUF servis par llama.cpp. Voir [models.md](models.md).

| Clé | Rôle |
|---|---|
| `name` | nom utilisé par les backends et llama-swap |
| `engine` | `gpu`, `cpu`, `whisper` (transcription, un seul) ou `tts` (voix Piper, un seul ; son `.onnx.json` est téléchargé aussi, vérifié par `config_sha256`) |
| `repo`, `file`, `sha256` | origine Hugging Face et empreinte vérifiée par `make models` |
| `ctx_size`, `parallel` | contexte et requêtes simultanées |
| `n_gpu_layers` | couches sur le GPU (99 = toutes) |
| `threads` | threads CPU (sinon `models.<engine>.default_threads`) |
| `flash_attn`, `cache_type_k`, `cache_type_v` | économie de VRAM (`q8_0` divise le cache KV par deux environ) |
| `mlock` | verrouille le modèle en RAM |
| `ttl_s` | déchargement après inactivité (0 = jamais) |
| `extra_args` | options supplémentaires de `llama-server` |

Par moteur (`models.gpu`, `models.cpu`) : `default_threads`, `health_timeout_s` et `swap` (`true` = un
seul modèle chargé à la fois ; `false` = tous les modèles du moteur restent chargés ensemble, utilisé
par le préréglage `cpu-only`).

Après modification : `make engines` (génère `engines/llama-swap-*.yaml`), `make models`, `make restart`.

### `store`
`path` de la base SQLite (`/data/agent.db` dans le conteneur via le volume `./data`) et
`retention_days` (180) : chaque jour, les conversations, interactions, entrées d'audit et tâches
terminées plus anciennes sont supprimées (0 = jamais). Les dépenses cloud sont gardées au moins 400
jours pour les plafonds mensuels.

## Recherche Internet (`web_tools`)

Le modèle CPU (et le cloud) peut chercher sur Internet (`web_search`) et lire une page
(`web_fetch`). La recherche passe par un **SearXNG auto-hébergé** : aucune clé d'API, aucun compte.

1. `.env` : `SEARXNG_SECRET` (généré par `make init`, sinon `openssl rand -hex 32`).
2. Démarrer le service : `make up PROFILE=full,web` (ou `COMPOSE_PROFILES=full,web docker compose up -d`).
   SearXNG n'a **aucun port publié** : seul l'agent le joint, par le réseau Compose. Sa
   configuration est `deploy/searxng/settings.yml` (format JSON activé, limiteur coupé).
3. `config.yaml` :

   ```yaml
   web_tools:
     enabled: true
     searxng_url: http://searxng:8080
   ```

4. Essayer : `/deep cherche la dernière version de Home Assistant et résume les nouveautés`.

Garde-fous de `web_fetch` : `http`/`https` seulement, pas d'identifiants dans l'URL ; le nom est
résolu et **toutes** ses adresses doivent être publiques (refus de `localhost`, du réseau local,
`10/8`, `172.16/12`, `192.168/16`, `100.64/10`, lien local et métadonnées `169.254.169.254`, IPv6
locales…) ; la connexion part vers l'adresse vérifiée (pas de seconde résolution DNS) ; chaque
redirection est revérifiée (3 au plus) ; taille et durée limitées ; seuls les textes (HTML, texte,
JSON, CSV, XML) sont lus, HTML réduit au texte visible. Les résultats portent l'avertissement « données
non fiables » et le modèle web n'a aucun outil de la maison.

## Ce qui se recharge à chaud

| Modifié depuis le panneau ou l'éditeur | Pris en compte |
|---|---|
| permissions, confirmations, politique par défaut | immédiatement |
| alertes et routines (`proactive`) | immédiatement |
| routage, mots-clés, budget, tarifs | immédiatement |
| allowlist Telegram | immédiatement |
| backends, URL HA, token, panneau, modèles | au redémarrage (`make restart`) |

Une modification **à la main** de `config.yaml` est détectée en quelques secondes et rechargée ; si
le fichier est invalide, l'erreur est journalisée et la configuration précédente reste active.
