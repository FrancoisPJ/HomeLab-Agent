# Domotique proactive : alertes et routines

L'agent ne fait pas que répondre : il peut **te prévenir** (fuite d'eau, porte restée ouverte,
température anormale) et **exécuter des routines** à heure fixe (vérification du soir, point du
matin). Tout se configure dans la section `proactive` de `config.yaml`, se modifie sans redémarrage
(panneau → Configuration) et se consulte dans le panneau (page **Proactif**) ou sur Telegram.

```yaml
proactive:
  enabled: true
  notify_chat_ids: []                 # vide = telegram.allowed_user_ids
  quiet_hours: { start: "23:00", end: "07:00" }
  alerts: [...]
  routines: [...]
```

## Alertes

Une alerte surveille une ou plusieurs entités (motif `*` accepté) et part quand sa condition
**devient vraie** et le **reste `for_s` secondes**. Les changements d'état arrivent en direct par la
WebSocket de Home Assistant.

| Clé | Rôle |
|---|---|
| `name` | identifiant (minuscules, chiffres, `_`) |
| `entity` | entity_id ou motif (`binary_sensor.porte_*`) |
| `state` | liste d'états qui déclenchent (`"on"`, `["open", "opening"]`) |
| `above` / `below` | seuils numériques (capteurs) |
| `for_s` | durée minimale de la condition (0 = immédiat) |
| `severity` | `info`, `warning` ou `critical` |
| `message` | texte ; variables `{name}` `{entity_id}` `{state}` `{unit}` `{area}` `{alert}` |
| `cooldown_s` | délai minimum entre deux notifications pour la même entité (3600 par défaut) |
| `action` | bouton sous le message : `{label, domain, service, entity}` |

Comportement :

- **Heures calmes** (`quiet_hours`) : seules les alertes `critical` sont envoyées ; les autres sont
  notées « heures calmes » dans le journal.
- **Anti-répétition** : une alerte qui repasse fausse puis vraie dans le `cooldown_s` n'est pas
  renvoyée.
- **Au démarrage**, les conditions déjà vraies (porte ouverte avant le redémarrage) sont réarmées ;
  l'anti-répétition est gardé en base, donc un redémarrage ne renvoie pas une alerte déjà envoyée.
- **Bouton d'action** : à usage unique, valable `action_ttl_s` (1 h), uniquement dans la conversation
  qui l'a reçu. L'action passe par **le même moteur de permissions** que tout le reste (et par la
  confirmation Oui/Non si elle est configurée). Sans `entity`, la cible est l'entité qui a déclenché
  l'alerte.
- Chaque déclenchement (envoyé, heures calmes, anti-répétition) est inscrit au **journal d'audit**
  (`alert:<nom>`).
- Les noms d'entités sont nettoyés avant d'être affichés (ce sont des données venues de Home
  Assistant).

Exemples :

```yaml
alerts:
  - name: fuite_eau
    entity: "binary_sensor.fuite_*"
    state: "on"
    severity: critical
    message: "💧 Fuite détectée : {name} ({area})"
    action: { label: "Couper l'eau", domain: valve, service: close_valve, entity: valve.arrivee_eau }
  - name: porte_garage
    entity: binary_sensor.porte_garage
    state: "on"
    for_s: 600
    message: "🚪 {name} est ouverte depuis 10 min"
    action: { label: "Fermer", domain: cover, service: close_cover, entity: cover.garage }
  - name: congelateur
    entity: sensor.temperature_congelateur
    above: -12
    for_s: 1800
    severity: critical
    message: "🧊 Congélateur à {state}{unit} depuis 30 min"
  - name: batterie_faible
    entity: "sensor.*_batterie"
    below: 15
    severity: info
    cooldown_s: 86400
    message: "🔋 {name} : {state}{unit}"
```

## Routines

### Les créer depuis Telegram

Écris simplement ce que tu veux, en langage naturel (ou en vocal) :

- « rappelle-moi de sortir les poubelles tous les mardis à 20h » → **rappel** (texte envoyé tel
  quel, sans LLM) ;
- « demain à 8h rappelle-moi le dentiste » → rappel **ponctuel** : il s'exécute une fois puis
  disparaît ;
- « tous les soirs à 23h, vérifie ce qui est encore allumé » → **vérification** sans LLM ;
- « en semaine à 7h30, fais-moi un point sur la maison » → **demande** traitée par l'agent.

L'agent prépare la routine puis demande **toujours** une confirmation (✅ Oui / ❌ Non) avec son
résumé : « Créer la routine sortir_les_poubelles : rappel « Sortir les poubelles », mar à 20:00 ? ».
Après validation, elle est écrite dans `config.yaml` (source de vérité unique) et active tout de
suite ; `proactive.enabled` passe à `true` si besoin. Une routine créée depuis une conversation est
envoyée à cette conversation.

Tu peux aussi demander « quelles sont mes routines ? », « mets en pause la vérification du soir » ou
« supprime le rappel poubelles » (toujours avec confirmation), ou utiliser les commandes :

| Commande | Effet |
|---|---|
| `/routines` | liste et prochaine exécution |
| `/run <nom>` | exécuter maintenant |
| `/routine off <nom>` / `/routine on <nom>` | mettre en pause / réactiver |
| `/routine delete <nom>` | supprimer |

Garde-fous : confirmation obligatoire pour toute création, pause ou suppression demandée au LLM ;
une routine de type « demande » **ne peut pas** créer ou modifier de routines (pas de planification
qui se reproduit) ; nombre maximal `proactive.max_routines` (50) ; `proactive.chat_edit: false`
retire complètement ces outils au LLM. Chaque exécution est inscrite au journal d'audit.

### Les écrire dans la configuration

| Clé | Rôle |
|---|---|
| `name` | identifiant |
| `at` | heure locale `HH:MM` (fuseau `agent.timezone`) |
| `days` | jours (`mon` … `sun`), tous par défaut |
| `date` | `AAAA-MM-JJ` : exécution unique ce jour-là, puis suppression automatique |
| `check` | rapport **sans LLM** : entités lisibles des `domains` dont l'état est dans `states` (et `entities` en motifs, facultatif) |
| `only_if_found` | `check` : rester silencieux si rien ne correspond (par défaut) |
| `prompt` | demande envoyée à l'agent comme si tu l'écrivais |
| `message` | rappel envoyé tel quel (« ⏰ … »), sans LLM |
| `chat_ids` | destinataires propres (sinon ceux de `proactive`) |
| `tier` | niveau pour `prompt` : `gpu` (réponse immédiate), `cpu` (tâche de fond), `cloud` |
| `enabled` | désactiver sans supprimer |

Une routine a **un seul** des trois : `check`, `prompt` ou `message`.

```yaml
routines:
  - name: verif_soir
    at: "23:00"
    check: { domains: [light, switch, cover, lock], states: ["on", "open", "unlocked"] }
  - name: brief_matin
    at: "07:30"
    days: [mon, tue, wed, thu, fri]
    prompt: "Point rapide : températures, ce qui est allumé ou ouvert, et s'il faut aérer."
    tier: gpu
  - name: poubelles
    at: "20:00"
    days: [tue]
    message: "Sortir les poubelles 🗑️"
  - name: rapport_hebdo
    at: "10:00"
    days: [sun]
    prompt: "Résume l'activité de la semaine de la maison à partir de l'historique des capteurs principaux."
    tier: cpu
```

**Agent arrêté au mauvais moment.** Un rappel ponctuel manqué est envoyé au redémarrage avec la
mention « en retard » s'il date de moins de `missed_grace_s` (2 h) ; au-delà, il est supprimé et tu
reçois « ⚠️ Routine manquée ». Les routines répétées reprennent simplement à la prochaine échéance.

Les routines `check` respectent les permissions de lecture : une entité masquée (`deny`) n'apparaît
jamais dans un rapport.

## Commandes Telegram

| Commande | Effet |
|---|---|
| `/alerts` | alertes configurées et derniers déclenchements |
| `/routines` | routines et prochaine exécution |
| `/run <nom>` | lancer une routine tout de suite (pour tester) |
| `/routine on\|off\|delete <nom>` | gérer une routine |

## Recette sur le vrai matériel

- [ ] Ouvrir une porte/fenêtre surveillée : message après `for_s`, pas de doublon pendant `cooldown_s`.
- [ ] La nuit, une alerte `warning` ne réveille personne ; une `critical` passe.
- [ ] Le bouton d'action agit une fois ; une entité en `read_only` le fait échouer proprement.
- [ ] `/run verif_soir` liste ce qui est allumé.
- [ ] « rappelle-moi de … tous les mardis à 20h » : le modèle rapide prépare la bonne routine
      (heure, jours, texte) ; noter les échecs pour choisir le modèle (`bench.py`).
