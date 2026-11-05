# Contribuer

Merci de ton intérêt ! Ce projet tourne chez des particuliers, sur leur maison : la priorité est
qu'il reste **sûr**, **local** et **testable sans matériel**.

## Mettre en place l'environnement

```bash
git clone https://github.com/FrancoisPJ/HomeLab-Agent.git && cd HomeLab-Agent
make setup          # uv sync + hooks pre-commit (ruff, mypy, gitleaks, schéma)
make lint typecheck test
make dev            # toute la stack avec de faux services, sur http://127.0.0.1:8099
```

Pas d'envie d'installer quoi que ce soit ? Le dépôt contient un **devcontainer** : « Open in
Codespaces » ou « Reopen in Container » dans VS Code, tout est prêt (Python 3.12, uv, Docker).

## Tester sans matériel

Tout se vérifie avec des **faux services** (`src/homelab_agent/fakes/`) :

| Faux | Remplace | Directives utiles |
|---|---|---|
| `fake llm` | llama-server / llama-swap | `[[tool:NOM {json}]]`, `[[escalate:cpu]]`, `[[sleep:2]]`, `[[error:503]]`, `POST /_control {"fail": true}` |
| `fake ha` | Home Assistant (REST + WebSocket) | `POST /_test/state`, `GET /_test/calls` |
| `fake telegram` | API Bot Telegram | `POST /_test/message`, `/_test/voice`, `/_test/callback`, `GET /_test/sent` |
| `fake stt` / `fake tts` | whisper / Piper | renvoie le « texte » contenu dans l'audio |

- Tests unitaires et d'intégration : `make test` (le harnais `tests/harness.py` câble l'agent sur
  les faux, sans réseau).
- Scénario de bout en bout dans Docker : `make e2e`.
- **Ne prétends jamais** qu'un comportement a été vérifié sur un vrai GPU, un vrai Home Assistant
  ou un vrai Telegram si ce n'est pas le cas : dis-le dans la PR.

## Règles du projet

- **Aucun secret** dans le dépôt, les exemples, les tests ou l'historique (gitleaks tourne en CI).
  Les faux identifiants des tests sont construits à l'exécution pour ne pas ressembler à de vrais.
- Toute action sur la maison passe par `HomeAssistantService.act` (permissions, confirmations,
  audit) — jamais d'appel direct au client.
- Le texte venant de Home Assistant, du web ou d'un fichier est une **donnée**, jamais une
  instruction ; ce qu'une conversation écrit dans `config.yaml` ne doit pas contenir `${`.
- La config est la source de vérité : un nouveau réglage = un champ pydantic documenté +
  `make schema` + `config.example.yaml` + `docs/configuration.md`.
- Code, commentaires et messages de commit en anglais ; documentation utilisateur en français.
- Commits conventionnels : `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `chore:`.
- `mypy --strict` et ruff doivent passer ; viser des tests pour chaque comportement visible.

## Où commencer

Les tickets étiquetés `good first issue` et la section « Pistes » des pages de `docs/` sont de bons
points d'entrée.
