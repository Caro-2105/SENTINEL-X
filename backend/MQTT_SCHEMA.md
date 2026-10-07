# Spécification des Données (Contrat MQTT)

Ce document décrit le format exact des données que le Backend Python s'attend à recevoir depuis le boîtier ESP8266 (IoT) via le serveur MQTT.

## Topic MQTT
**Topic d'écoute du Backend** : `sentinel/sensors`

## Format de réception : Objet JSON
Le backend s'attend à recevoir une chaîne de caractères au format **JSON valide**.

### Schéma attendu par le Backend

| Clé JSON | Type JSON attendu | Type Python résultant | Description | Valeurs typiques |
| :--- | :--- | :--- | :--- | :--- |
| `temperature` | `Number` (Float) | `float` | Température ambiante captée par le DHT11. | `22.5` |
| `humidite` | `Number` (Float) | `float` | Taux d'humidité capté par le DHT11. | `45.0` |
| `gaz` | `Number` (Integer) | `int` | Valeur analogique brute du capteur MQ-2. | `150` à `1023` |
| `presence` | `Number` (Integer) | `int` | État du détecteur de mouvement PIR. | `0` (Rien) ou `1` (Mouvement) |
| `rfid_uid` | `String` ou `null` | `str` ou `None` | UID du badge s'il vient d'être scanné. | `"4A E1 8B 00"` ou `null` |
| `ir_temp` | `Number` (Float), **optionnel** | `float` ou `None` | Température infrarouge de la cible (°C), ex. MLX90614. Absent => le backend se rabat sur `presence`. | `34.5` |

> `gaz` vient du MQ-2 qui mesure gaz combustibles **et** fumées sur une seule voie analogique : la même valeur sert aux deux risques.
> `temperature` / `humidite` peuvent valoir `null` si le DHT22 échoue à la lecture.

---

### Exemple de Payload reçu (Ce que `msg.payload.decode()` va lire)

```json
{
  "temperature": 23.5,
  "humidite": 50.2,
  "gaz": 120,
  "presence": 1,
  "rfid_uid": null
}
```

### Exemple de traitement dans le Backend Python
Une fois le message reçu par le backend, voici comment il sera traité :

```python
import json

payload_text = '{"temperature": 23.5, "humidite": 50.2, "gaz": 120, "presence": 1, "rfid_uid": null}'

# Conversion du texte JSON en Dictionnaire Python
data = json.loads(payload_text)

# Le backend accède alors aux données avec les types natifs Python :
temp = data["temperature"] # type: float
is_intruder = data["presence"] == 1 # type: bool
```


---

## Topic `sentinel/vision` (script caméra -> backend)

Publié par `vision.py` environ 1 fois par seconde.

| Clé JSON | Type | Description |
| :--- | :--- | :--- |
| `label` | `String` ou `null` | Classe du modèle : prénom du membre, `"inconnu"` (classe *Personne*) ou `null` (classe *Vide*, personne devant la caméra). |
| `confiance` | `Number` 0..1 | Confiance de la prédiction. Sous 0.80 le visage n'est pas accepté (l'écran affiche « CONFIANCE TROP FAIBLE » et le taux). |
| `connu` | `Boolean` | `true` si le modèle IA considère la personne comme membre de l'équipe. |
| `visages` | `Integer` | Nombre de visages détectés dans l'image (0 = personne). |

```json
{"label": "Caroline", "confiance": 0.93, "connu": true, "visages": 1}
```

## Topic `sentinel/commandes` (backend -> ESP8266)

Publié à chaque changement d'état et au moins toutes les 2 s (l'ESP repasse en mode neutre après 15 s sans ordre).

| Clé JSON | Type | Description |
| :--- | :--- | :--- |
| `buzzer` | `Integer` | `0` silencieux, `1` bip intermittent (anomalie), `2` alarme continue rapide (critique / intrusion). |
| `ligne1` / `ligne2` | `String` | Texte OLED, ASCII sans accents, 21 caractères maximum. |

```json
{"buzzer": 0, "ligne1": "ACCES AUTORISE", "ligne2": "Bienvenue Alice"}
```

## Stories gérées (moteur `backend/scenarios.py`)

| Code | Condition | Buzzer | Écran |
| :--- | :--- | :---: | :--- |
| ACC-01 | présence + visage connu (conf. >= 0.80) + chaleur humaine | 0 | ACCES AUTORISE / Bienvenue <label> |
| ACC-02 | présence + visage inconnu / membre désactivé | 2 | INTRUSION / Personne inconnue |
| ACC-03 | présence, pas encore de visage (< 8 s) | 0 | IDENTIFICATION / Regardez la camera |
| ACC-04 | présence, aucun visage exploitable après 8 s | 1 | ALERTE PRESENCE / Non identifie |
| ACC-05 | présence + visage connu mais `ir_temp` hors 28-42 °C (photo / écran) | 2 |
| ACC-06 | présence + visage connu mais confiance < 0.80 | 1 | CONFIANCE TROP FAIBLE / Taux : <valeur>% | ACCES REFUSE / Chaleur absente |
| ENV-01 | IA environnement : niveau 1 (anomalie, dérive) | 1 | ATTENTION / <catégorie> |
| ENV-02 | IA environnement : niveau 2 (critique) | 2 | ALERTE DANGER / <catégorie> |

Les scénarios Accès et Environnement sont indépendants : un membre autorisé présent pendant une fuite de gaz reste accepté, mais le buzzer et l'écran passent en alerte.

## Caméra pilotée par la présence (vision.py <-> backend <-> front)

La caméra n'est allumée que si le PIR est actif (maintien 10 s après la dernière détection) ou si le front la force.

| Topic | Sens | Payload |
|---|---|---|
| `sentinel/camera/state` | vision.py -> backend (1/s, non retenu) | `{"actif": bool, "mode": "auto"\|"manuel", "erreur": str\|null, "flux": "http://localhost:8001/stream"}` |
| `sentinel/camera/cmd` | backend -> vision.py | `{"action": "on"}` (forcer, 5 min max) ou `{"action": "auto"}` (piloté par le PIR) |

API : `POST /api/camera` `{"action": "on"|"auto"}` ; `GET /api/status` contient `camera` (`en_ligne` = false si aucun état reçu depuis 5 s).
Le flux MJPEG (`/stream`, `/snapshot.jpg`) n'écoute que sur 127.0.0.1 : il n'est visible que depuis le PC qui exécute vision.py.
Variables : `CAMERA_INDEX` (défaut 1), `CAMERA_STREAM_PORT` (8001), `FACE_WINDOW=1` pour garder aussi la fenêtre OpenCV.

## Webcam du poste (authentification du tableau de bord, vision_poste.py)

Séparée de la caméra de la porte : ces messages n'alimentent jamais les scénarios d'accès.

| Topic | Sens | Payload |
|---|---|---|
| `sentinel/poste/cmd` | backend -> vision_poste.py | `{"action": "on"}` (bail de 90 s, renouvelé à chaque demande) ou `{"action": "off"}` |
| `sentinel/poste/vision` | vision_poste.py -> backend | `{"label", "confiance", "connu", "visages"}` (même format que `sentinel/vision`) |
| `sentinel/poste/state` | vision_poste.py -> backend (1/s) | `{"actif": bool, "erreur": str\|null, "flux": "http://127.0.0.1:8101/stream?k=<clé>"}` |
