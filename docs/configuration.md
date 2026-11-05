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
| `openai_compat` | `url` (avec `/v1`), `api_key_env` (facultatif), `max_context` (doit correspondre au `ctx_size` du modèle) |
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
`url`, `token_env`, `verify_ssl`, `default_policy`, `rules`, `confirm`, `context_max_chars`,
`context_domains`, `request_timeout_s`. Voir [home-assistant.md](home-assistant.md).

### `web`
`enabled`, `bind` (127.0.0.1), `port`, `password_env`. Dans Docker, le conteneur écoute sur
`0.0.0.0` (variable `HLA_WEB_BIND`) mais le port n'est publié que sur `127.0.0.1` de l'hôte.

### `voice`
Messages vocaux Telegram : `enabled`, `stt_url`, `language`, `max_duration_s`, `echo_transcript`,
`timeout_s`, `stt_model`, `api_key_env`. Voir [voice.md](voice.md).

### `proactive`
Alertes sur événements Home Assistant et routines planifiées (`enabled`, `notify_chat_ids`,
`quiet_hours`, `alerts`, `routines`, `action_ttl_s`, `chat_edit`, `max_routines`). Les routines
peuvent être créées depuis Telegram, après confirmation. Voir [proactive.md](proactive.md).

### `models`
Les GGUF servis par llama.cpp. Voir [models.md](models.md).

| Clé | Rôle |
|---|---|
| `name` | nom utilisé par les backends et llama-swap |
| `engine` | `gpu`, `cpu` ou `whisper` (modèle de transcription vocale, un seul) |
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
`path` de la base SQLite (`/data/agent.db` dans le conteneur via le volume `./data`).

## Ce qui se recharge à chaud

| Modifié depuis le panneau ou l'éditeur | Pris en compte |
|---|---|
| permissions, confirmations, politique par défaut | immédiatement |
| alertes et routines (`proactive`) | immédiatement |
| routage, mots-clés, budget, tarifs | immédiatement |
| allowlist Telegram | immédiatement |
| backends, URL HA, token, panneau, modèles | au redémarrage (`make restart`) |
