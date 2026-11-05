# homelab-agent

Toutes les nouveautés de la version : voir [CHANGELOG.md](https://github.com/FrancoisPJ/HomeLab-Agent/blob/main/CHANGELOG.md).

**En bref (0.5.0)** : photos des caméras dans Telegram (`/camera salon`, ou « montre-moi
l'entrée »), désactivées par défaut et jamais montrées à un modèle ; supervision Prometheus +
Grafana (profil `monitoring`, tableau de bord fourni) ; panneau web redessiné (« liquid glass »,
palette de commandes Ctrl K, barre d'onglets sur téléphone) ; mêmes commandes `/…` dans le chat du
panneau que sur Telegram ; `make init` génère les mots de passe ; passe de qualité du code
(panneau découpé par page, fonctions longues découpées, complexité plafonnée, couverture 91 %).

Images publiées sur GitHub Container Registry : `homelab-agent-agent`, `homelab-agent-llama-cpu`,
`homelab-agent-llama-gpu` (CUDA 12.2, sm_86), `homelab-agent-whisper`, `homelab-agent-tts`,
`homelab-agent-fakes`.

**Testé en CI** : lint (avec plafond de complexité), `mypy --strict`, ~250 tests unitaires et
d'intégration avec faux services, tableau de bord Grafana et fichier Compose vérifiés, scénario e2e
Compose (16 étapes, dont la photo de caméra), construction des 6 images, gitleaks, CodeQL.

**Vérifié pendant le développement, sur la stack de dev (services simulés)** : Prometheus collecte
l'agent avec le jeton, Grafana affiche les 19 panneaux avec des données ; panneau dans un vrai
navigateur (Chromium) en clair, sombre et taille téléphone, sans erreur JavaScript ; photo de
caméra de bout en bout dans les conteneurs (faux Home Assistant → adaptateur aiogram → fausse API
Telegram).

**Non testé** : vraies caméras et vrai Home Assistant, vrai Telegram, GPU réel, vitesses sur
l'i9 / RTX 3070, vraies API cloud, Safari et Firefox pour le panneau (la réfraction de la barre est
réservée aux navigateurs Chromium, les autres ont un verre dépoli). Voir la checklist de recette
dans `docs/deployment.md`.
