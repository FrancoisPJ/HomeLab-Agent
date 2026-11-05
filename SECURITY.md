# Sécurité

homelab-agent pilote une maison : une faille peut avoir des conséquences physiques (serrure,
alarme, chauffage). Merci de la signaler de façon responsable.

## Signaler une vulnérabilité

- **En privé**, via *Security → Report a vulnerability* sur GitHub
  (<https://github.com/FrancoisPJ/HomeLab-Agent/security/advisories/new>).
- Pas de ticket public, pas de démonstration contre une installation qui n'est pas la tienne.
- Décris le scénario, la version (tag ou commit), et si possible un test qui reproduit le problème
  avec les faux services (`tests/harness.py`).

Réponse visée sous 7 jours. Seule la dernière version publiée reçoit des correctifs.

## Modèle de menace (résumé)

| Surface | Protection |
|---|---|
| Telegram | liste blanche d'identifiants numériques ; autres utilisateurs ignorés sans réponse ; seul l'auteur d'une demande peut la confirmer |
| Panneau web | écoute sur `127.0.0.1` ; mot de passe obligatoire (refus de démarrer sinon) ; limitation des essais ; cookie `SameSite=Strict` |
| Moteurs llama.cpp / whisper / TTS | aucune authentification : jamais publiés hors du réseau Compose |
| Actions Home Assistant | moteur de permissions dans le code (pas dans le prompt), cible explicite obligatoire, services d'administration bloqués, confirmation Oui/Non configurable, journal d'audit |
| Injection d'instructions | les données Home Assistant / web sont nettoyées et marquées comme données ; les routines et la mémoire créées depuis une conversation sont visibles et ne peuvent pas écrire de `${…}` dans la config |
| Secrets | uniquement en variables d'environnement, masqués dans les journaux, gitleaks en CI |
| Chaîne d'approvisionnement | versions épinglées, empreintes SHA256 vérifiées (modèles, llama-swap), commits de llama.cpp / whisper.cpp vérifiés, Dependabot, CodeQL, scan d'images |

Risques acceptés et documentés : voir `docs/troubleshooting.md` (vulnérabilité connue de llama-swap).
