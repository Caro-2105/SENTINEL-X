# Dialogue back ↔ firmware ESP (version actuelle du firmware de l'équipe)

Le firmware fait tourner un mini-broker MQTT (uMQTTBroker). Le pont `backend/pont_esp.py` s'y connecte en client.

## Lecture des capteurs (PC → ESP, puis réponse)
| Demande (le contenu est ignoré) | Commande série envoyée à la carte capteurs | Réponse |
|---|---|---|
| `esp/requetes/temp` | `TEMP` | `esp/response/temp` → `{"result":"24.4"}` |
| `esp/requetes/dist` | `DIST` | `esp/response/dist` |
| `esp/requetes/hum`  | `HUM`  | `esp/response/hum` |
| `esp/requetes/gaz`  | `GAZ`  | `esp/response/gaz` |

Le nom après `esp/requetes/` est mis en majuscules et envoyé tel quel à la carte capteurs : un nom qu'elle ne
connaît pas fait attendre 2 s (timeout) et bloque l'ESP.

## Écran et sons (PC → ESP) : `esp/function/*`
| Topic | Contenu | Effet |
|---|---|---|
| `esp/function/screen` | `{"str":"<ligne 1 sur 16 car.><ligne 2>"}` (32 car. max, sans guillemet) | écran effacé puis écrit + bip court ; `{"str":""}` = écran vide, silencieux |
| `esp/function/granted` | (vide) | mélodie « accès accordé » |
| `esp/function/denied` | (vide) | mélodie « accès refusé » |

Le back pense en niveaux de buzzer 0 / 1 / 2 ; `backend/ordres_esp.py` les traduit : écran réécrit à chaque
changement d'état seulement, « denied » répété tant qu'une alerte dure (toutes les 3 s en danger, 10 s en anomalie),
« granted » à l'accès autorisé. Le firmware n'a pas d'alarme continue.

## Présence
Le firmware publie lui-même `esp/presence` (`{"result":"<cm>"}`) quand la distance passe sous 50 cm. Le pont n'en a pas
besoin : il interroge `dist` et applique son propre seuil (`--esp-seuil`, 60 cm par défaut).

## Ancien firmware (topics `esp/request/*`, pas d'écran)
`ESP_TOPIC_REQUETES=esp/request` rétablit les anciens topics de lecture.
