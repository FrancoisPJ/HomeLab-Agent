# Cloud et budget

## Par défaut, rien n'est dépensé

`cloud.enabled: false` et tous les plafonds à `0`. Tant que vous ne changez pas ces valeurs, aucune
requête n'est envoyée aux API Claude ou Gemini ; `/cloud` répond que le cloud est désactivé et la
demande est traitée en local.

## Abonnement ≠ API

Les **abonnements grand public** (Claude Pro/Max, Gemini Advanced, offres étudiantes…) **ne donnent
pas de clé API**. L'API se paie séparément, à l'usage :

- Anthropic : créez une clé dans la [Console Anthropic](https://console.anthropic.com/) ; tarifs sur
  la [page des prix](https://www.anthropic.com/pricing) ; identifiants de modèles dans la
  [documentation des modèles](https://docs.anthropic.com/en/docs/about-claude/models).
- Google : créez une clé dans [Google AI Studio](https://aistudio.google.com/apikey) ; tarifs sur la
  [page des prix Gemini API](https://ai.google.dev/gemini-api/docs/pricing) ; il existe un **palier
  gratuit** dont les limites et conditions (dont l'usage des données) sont décrites sur les pages
  [tarifs](https://ai.google.dev/gemini-api/docs/pricing) et
  [limites de débit](https://ai.google.dev/gemini-api/docs/rate-limits) — vérifiez-les, elles changent.

Ce dépôt ne fige aucun tarif ni nom de modèle : ils évoluent.

## Activer

```yaml
backends:
  claude: { kind: anthropic, api_key_env: ANTHROPIC_API_KEY, model: "<identifiant du modèle>" }
  gemini: { kind: gemini, api_key_env: GEMINI_API_KEY, model: "<identifiant du modèle>" }
routing:
  tiers:
    cloud: [claude, gemini]        # ordre = préférence ; le second sert de repli
cloud:
  enabled: true
  pricing:                         # € par million de jetons, recopiés depuis les pages officielles
    claude: { in_per_mtok: 0.0, out_per_mtok: 0.0 }
    gemini: { in_per_mtok: 0.0, out_per_mtok: 0.0 }
  limits:
    daily_eur: 1
    monthly_eur: 10
    alert_at: 0.8
    per_provider:
      claude: { daily_eur: 0.5, monthly_eur: 5 }
```

Les prix sont souvent publiés en dollars : convertissez, ou acceptez que le compteur soit approximatif.
Si `pricing` manque pour un fournisseur, son coût est compté **0** : renseignez-le toujours.

## Fonctionnement

- Après chaque réponse cloud, les jetons d'entrée/sortie rapportés par l'API sont multipliés par les
  tarifs et ajoutés à la table `spend`.
- Avant chaque appel, les plafonds global et par fournisseur (jour et mois, selon `agent.timezone`)
  sont comparés aux dépenses. Plafond atteint → fournisseur suivant, puis repli local, avec un message
  explicite (« plafond journalier atteint (total : 1,02 € / 1,00 €) → bascule sur CPU lourd »).
- Une alerte Telegram part une seule fois par période quand `alert_at` (80 %) puis 100 % sont atteints.
- Le dépassement peut être d'une requête : le contrôle se fait avant l'appel, le coût est connu après.

Page **Budget** du panneau : dépenses du jour et du mois par fournisseur, plafonds, tarifs.
