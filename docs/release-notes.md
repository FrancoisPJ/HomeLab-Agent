# homelab-agent

Images publiées sur GitHub Container Registry : `homelab-agent-agent`, `homelab-agent-llama-cpu`,
`homelab-agent-llama-gpu` (CUDA 12.2, sm_86), `homelab-agent-fakes`.

**Testé en CI** : lint, types, tests unitaires et d'intégration avec faux backends, scénario e2e Compose
avec faux LLM / Home Assistant / Telegram, construction des images, scan gitleaks.

**Non testé** : exécution sur GPU réel, vitesses, vrai Home Assistant, vrai Telegram, vraies API cloud.
Voir la checklist de recette dans `docs/deployment.md`.
