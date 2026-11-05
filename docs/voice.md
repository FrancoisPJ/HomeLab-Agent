# Messages vocaux

Envoie un **message vocal** au bot Telegram : il est transcrit **en local** par
[whisper.cpp](https://github.com/ggml-org/whisper.cpp), affiché (« 🎤 « éteins le salon » »), puis
traité exactement comme un message écrit (chemin rapide, routage, permissions…). Aucun audio ne sort
de chez toi.

```
Telegram (OGG/Opus) ─► agent ─► whisper (ffmpeg → WAV → whisper.cpp) ─► texte ─► orchestrateur
```

## Activer

1. `config.yaml` :
   ```yaml
   voice:
     enabled: true
     stt_url: http://whisper:8080/v1/audio/transcriptions
     language: fr
     max_duration_s: 120
     echo_transcript: true
   models:
     items:
       - name: stt
         engine: whisper
         repo: ggerganov/whisper.cpp
         file: ggml-small.bin
         sha256: 1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b
         threads: 4
   ```
2. `make engines models` (télécharge le modèle vocal, vérifie son SHA256, écrit `engines/whisper.env`).
3. `make up PROFILE=full,voice` (ou `cpu-only,voice`).

`/status` affiche une ligne « Voix (transcription) ».

## Choisir le modèle

Modèles officiels du dépôt `ggerganov/whisper.cpp` (licence MIT), vérifiés sur Hugging Face :

| Fichier | Taille | SHA256 | Remarque |
|---|---|---|---|
| `ggml-base-q8_0.bin` | 82 Mo | `c577b9a8…cbb7d9` | très rapide, français correct pour des phrases courtes |
| `ggml-small.bin` (défaut) | 488 Mo | `1be3a9b2…a987b` | bon compromis sur CPU |
| `ggml-small-q8_0.bin` | 264 Mo | `49c8fb02…f779f` | small quantifié, plus léger |
| `ggml-large-v3-turbo-q5_0.bin` | 574 Mo | `39422170…a7e2` | meilleure qualité, plus lent sur CPU |

La transcription tourne sur le **CPU** pour ne pas concurrencer le modèle GPU (et Jellyfin).
Réglez `threads` selon les cœurs libres.

**Mesure de référence (pas sur ton matériel)** : pendant le développement, la chaîne complète (image
`whisper`, conversion ffmpeg, `ggml-small.bin`, 4 vCPU de cloud) a transcrit un vocal OGG/Opus de
3 secondes en **2,5 s**. L'échantillon était une voix de synthèse (espeak-ng), mal articulée :
« Éteins la lumière du salon » est devenu « Et à la lumière du salon ». Teste avec ta propre voix et
compare les modèles.

## Comportement

- Utilisateur hors liste blanche : ignoré, rien n'est téléchargé.
- Vocal plus long que `max_duration_s` : refusé avec un message.
- Moteur vocal injoignable : « ❌ Transcription impossible », le message n'est pas perdu côté
  Telegram (tu peux le réécrire).
- `echo_transcript: false` : pas de ligne « 🎤 « … » », la réponse arrive directement.
- Le texte transcrit passe par les mêmes protections que du texte tapé (permissions, confirmations).

## Autres serveurs compatibles

`stt_url` accepte tout serveur exposant l'API `POST /v1/audio/transcriptions` (champ `file`,
réponse JSON `{"text": ...}`) : par exemple un serveur faster-whisper. `stt_model` est envoyé dans le
champ `model` et `api_key_env` ajoute un en-tête `Authorization` si besoin. N'utilise pas une API
cloud si tu veux garder l'audio chez toi.

## Réponses vocales (Piper)

L'agent peut aussi **répondre par une note vocale**, synthétisée en local par
[Piper](https://github.com/OHF-Voice/piper1-gpl) :

```yaml
voice:
  enabled: true
  reply_voice: when_voice     # never | when_voice (si tu as parlé) | always
  tts_url: http://tts:8080/v1/audio/speech
models:
  items:
    - name: voix
      engine: tts
      repo: rhasspy/piper-voices
      file: fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx
      sha256: 641d1ab097da2b81128c076810edb052b385decc8be3381814802a64a73baf99
      config_sha256: 39479916c2db192b5ac9764daddd0c744d83e023ad890c6976c0633ae4df8959
```

Puis `make engines models` et `make up PROFILE=full,voice` (le profil `voice` lance `whisper` et `tts`).

- La réponse texte arrive toujours ; la note vocale suit (OGG/Opus, le format des vocaux Telegram).
- Le texte est rendu « prononçable » : émojis, liens, code et marques de formatage retirés, longueur
  limitée (`tts_max_chars`), et les réponses techniques du chemin rapide sont dites en français
  (« ✅ light.turn_off → salon » devient « C'est fait. J'ai éteint « salon ». »).
- Si la synthèse échoue, seule la réponse texte est envoyée.
- Les réponses des tâches CPU différées ne sont pas lues.

**Mesures pendant le développement (4 vCPU de cloud, pas ton matériel)** : une phrase est
synthétisée en ~1,2 s. Boucle complète avec les vrais moteurs — vocal généré par Piper, transcrit
par whisper (`ggml-small`), chemin rapide, réponse synthétisée — en **3 s**. Whisper a retranscrit
mot pour mot « Éteins la lumière du salon, s'il te plaît. » et « La lumière du salon est éteinte. Il
fait 21° et demi. ». Sur des phrases très courtes, il se trompe parfois (« éteint » entendu « tant ») :
la tournure des confirmations a été choisie pour rester intelligible.

**Licences.** Piper est sous GPL-3.0 : il tourne dans son propre conteneur, comme un programme
séparé ; le code du projet (Apache-2.0) ne l'importe pas et lui parle en HTTP
(`docker/tts/server.py`). La voix par défaut **siwis** est sous CC-BY 4.0 (corpus SIWIS, université
d'Édimbourg, <https://datashare.is.ed.ac.uk/handle/10283/2353>) : l'attribution est requise si tu
diffuses des enregistrements. Autres voix françaises : `tom` (AGPLv3), `upmc` (CC-BY-SA 4.0) — lis
leur fiche (`MODEL_CARD`) avant de les utiliser.

## Pistes

- Transcription sur GPU quand la VRAM est libre (image whisper.cpp CUDA).
- Choisir la voix par conversation.
