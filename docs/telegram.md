# Telegram

## Créer le bot

1. Parlez à [@BotFather](https://t.me/BotFather) → `/newbot` → récupérez le token.
2. `.env` : `TELEGRAM_BOT_TOKEN=...`
3. Récupérez votre **identifiant numérique** (par exemple avec un bot comme @userinfobot) et mettez-le
   dans `telegram.allowed_user_ids`.

Le bot fonctionne en **long polling** : aucun port entrant, aucun webhook, aucun nom de domaine.

## Liste blanche

Tout utilisateur absent de `allowed_user_ids` est **ignoré sans réponse** ; la tentative est
journalisée (`ignored message from non-allowlisted user`) et comptée dans `/metrics`. Les clics sur
des boutons par un autre utilisateur sont aussi ignorés.

## Commandes

| Commande | Effet |
|---|---|
| `/start`, `/help` | aide |
| `/status` | état des moteurs, de Home Assistant, budget du jour et du mois, jobs actifs |
| `/model` | moteurs et modèles de chaque niveau |
| `/cloud <texte>` | force le cloud (si activé et dans le budget, sinon repli expliqué) |
| `/deep <texte>` | force le CPU : accusé immédiat, réponse plus tard |
| `/jobs` | dernières tâches de la conversation |
| `/reset` | efface l'historique de la conversation |
| `/maison` | télécommande à boutons (pièces → appareils), sans LLM |
| `/alerts`, `/routines`, `/run <nom>`, `/routine on\|off\|delete <nom>` | domotique proactive, voir [proactive.md](proactive.md) |

## Télécommande `/maison`

`/maison` affiche un clavier : une ligne de boutons par pièce (zones Home Assistant), puis, dans une
pièce, un bouton par appareil avec son état (« 💡 Lumière salon · allumé »). Un appui bascule
l'appareil (allumer/éteindre, ouvrir/fermer, verrouiller/déverrouiller, activer une scène…) et le
clavier se met à jour avec le nouvel état. Les capteurs de la pièce s'affichent en texte.

- Aucun LLM : c'est instantané et ça marche même si tous les moteurs sont arrêtés.
- Chaque appui passe par les **permissions** : un appareil en lecture seule s'affiche avec 🔒, un
  appareil soumis à confirmation avec ⚠️ (et la confirmation n'est acceptée que de la personne qui a
  appuyé). Tout est inscrit au journal d'audit (`home_remote`).
- Domaines affichés : `telegram.remote_domains` (lumières, prises, ventilateurs, volets, serrures,
  scènes, scripts, lecteurs, booléens, boutons). Plus de 10 appareils : pagination ◀️ ▶️.

## Streaming et limites

Les réponses s'affichent progressivement en **éditant un message**, au plus une édition toutes les
`stream_edit_interval_s` secondes (1,2 s par défaut) pour respecter les limites de l'API Bot (voir la
[FAQ officielle](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)).
Les erreurs « réessayer après N s » sont respectées. Les réponses longues sont découpées
(`max_message_chars`, 4 000 par défaut, sous la limite de 4 096 caractères d'un message).

## Confirmations

Pour une action soumise à confirmation, le bot envoie la question avec deux boutons **✅ Oui /
❌ Non**. Seul l'auteur de la demande peut répondre (utile dans un groupe). Sans réponse après
`confirm_timeout_s`, c'est un non.

## Messages vocaux

Avec `voice.enabled: true`, un vocal est téléchargé, transcrit en local, affiché
(« 🎤 « … » ») puis traité comme un message écrit. Voir [voice.md](voice.md).

## Phase 2

L'interface `Gateway` (`gateway/base.py`) permet d'ajouter WhatsApp ou une entrée vocale sans toucher
à l'orchestrateur : une gateway transforme les messages entrants en `IncomingMessage` et fournit un
`Replier` (envoyer, streamer, confirmer).
