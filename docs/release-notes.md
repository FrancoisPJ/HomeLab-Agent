# homelab-agent

Toutes les nouveautés de la version : voir [CHANGELOG.md](https://github.com/FrancoisPJ/HomeLab-Agent/blob/main/CHANGELOG.md).

**En bref (0.4.0)** : télécommande `/maison` dans Telegram, chat dans le panneau web, mémoire longue,
bouton ↩️ Annuler, alertes et routines (créées en langage naturel), messages vocaux transcrits en
local et réponses vocales (Piper), modèle GPU « qualité », préréglage `cpu-only`, et deux audits de
sécurité corrigés (services d'administration bloqués, cible obligatoire, pas de `${…}` venant d'une
conversation, confirmations liées à leur auteur, etc.).

Images publiées sur GitHub Container Registry : `homelab-agent-agent`, `homelab-agent-llama-cpu`,
`homelab-agent-llama-gpu` (CUDA 12.2, sm_86), `homelab-agent-whisper`, `homelab-agent-tts`,
`homelab-agent-fakes`.

**Testé en CI** : lint, `mypy --strict`, ~200 tests unitaires et d'intégration avec faux services,
scénario e2e Compose (15 étapes), construction des 6 images, gitleaks, CodeQL.

**Vérifié pour de vrai pendant le développement (pas sur le matériel cible)** : inférence CPU de
l'image `llama-cpu` avec un petit modèle, boucle vocale complète avec les vraies images `whisper` et
`tts` (≈ 3 s sur 4 vCPU), chat du panneau dans un vrai navigateur.

**Non testé** : GPU réel, vitesses sur l'i9 / RTX 3070, vrai Home Assistant, vrai Telegram, vraies
API cloud. Voir la checklist de recette dans `docs/deployment.md`.
