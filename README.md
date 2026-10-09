# SENTINEL-X

Prototype d'avant-poste industriel pour la société fictive AetherCorp, réalisé dans le cadre du Workshop Bac+4.
Équipe : Caroline Delcour, Florent Germain, Killian Pinte et Quentin Bullert.

## Principe

Un boîtier installé à une porte (ESP8266, carte capteurs, écran LCD, haut-parleur, caméra USB) détecte l'arrivée d'une personne, l'identifie par reconnaissance faciale et l'autorise ou non à entrer. Les personnes à l'intérieur utilisent un PC serveur qui héberge le tableau de bord (mesures en direct, courbes, journal d'événements, prévisions). Le boîtier mesure aussi l'environnement (température, humidité, gaz) et déclenche écran et buzzer en cas d'anomalie.

## Architecture

- **Boîtier (iot/)** : firmware C++ de l'ESP8266. Il embarque un mini-broker MQTT (port 1883), répond aux demandes de mesures, publie la présence et pilote l'écran et le haut-parleur.
- **Pont ESP (backend/pont_esp.py)** : interroge l'ESP, reformate les mesures et les publie sur le broker Mosquitto du PC. Il relaie aussi les ordres d'écran et de buzzer du serveur vers l'ESP.
- **Backend (backend/)** : API FastAPI, client MQTT, base PostgreSQL, moteur de scénarios, IA d'analyse des mesures, authentification, prévisions météo. Il sert aussi le tableau de bord.
- **IA (ai/)** : modèles Orange (reconnaissance faciale, variations des mesures) et leurs scripts d'entraînement.
- **Tableau de bord (frontend/)** : HTML et JavaScript (Chart.js), servi par le backend.
- **Infrastructure (infra/)** : Docker Compose (Mosquitto et PostgreSQL).

Arborescence : `iot/`, `ai/`, `backend/`, `frontend/`, `infra/`, `lancer.py` (lanceur unique) et `rapport_sentinel_x.md` (rapport de projet détaillé).

## Démarrage rapide

Prérequis : Python 3.10 ou plus (testé en 3.14), Docker Desktop lancé, accès Internet (embedder Inception v3 d'Orange, prévisions météo).

```bash
git clone https://github.com/Caro-2105/SENTINEL-X.git
cd SENTINEL-X
python -m pip install -r backend/requirements.txt
python lancer.py            # sous Windows : double-clic sur lancer.bat
```

Au premier lancement, `lancer.py` vérifie les bibliothèques et les modèles, démarre Docker (base et broker), fait choisir la caméra de la porte (USB externe) et celle du poste (webcam intégrée), puis demande un prénom et un mot de passe (8 caractères minimum, avec majuscules, minuscules et un chiffre). Il ouvre ensuite http://127.0.0.1:8000/ : reconnaissance du visage, mot de passe, puis tableau de bord. `Ctrl+C` arrête l'ensemble.

### Options du lanceur

| Commande | Effet |
|---|---|
| `--esp <IP>` | relaie les mesures de l'ESP (IP affichée sur son écran au démarrage) |
| `--esp-capteurs temp,dist,hum,gaz` | capteurs à interroger (défaut : `temp,dist`) |
| `--esp-seuil <cm>` | distance de détection de présence (défaut : 60) |
| `--esp-fahrenheit` | température de l'ESP en °F, convertie en °C |
| `--simulateur [scénario]` | joue un scénario sans matériel (`gaz`, `incendie`, etc.) |
| `--sans-env` | pas d'écran ni de buzzer pour les alertes d'environnement (capteurs et météo) |
| `--sans-meteo` | désactive les prévisions météo (aucun appel à Internet) |
| `--sans-porte` | pas de caméra du boîtier |
| `--sans-poste` | pas de caméra du poste |
| `--dev` | mode développeur : bouton d'entrée sans visage ni mot de passe, depuis ce PC seulement |
| `--reconfigurer` | choisir à nouveau les caméras |
| `--mot-de-passe` | changer son mot de passe |
| `--sans-docker`, `--sans-navigateur`, `--verbeux` | ne pas démarrer Docker, ne pas ouvrir le navigateur, afficher tous les logs |

Exemple avec le matériel : `python lancer.py --esp 10.235.154.64 --esp-capteurs temp,dist,hum,gaz`.

Le mode `--dev` contourne l'authentification : ne jamais l'utiliser pour une démonstration ou une livraison.

### Dépannage

- Docker non démarré : lancer Docker Desktop.
- Mauvaise caméra : `--reconfigurer` (les numéros diffèrent d'un PC à l'autre).
- Port 8000, 1883 ou 5433 déjà utilisé : fermer l'ancien processus.
- Variables d'environnement sous PowerShell : `$env:NOM="valeur"`.
- Mosquitto : `infra/mosquitto/config/mosquitto.conf` doit contenir `listener 1883 0.0.0.0` (l'adresse 0.0.0.0, pas l'IP du PC).

### Tests

```bash
cd backend
python -m unittest discover -s tests -v
```

## Boîtier ESP8266

Firmware : `iot/sentinel_x_esp_broker.ino` (bibliothèques ESP8266WiFi, uMQTTBroker, LiquidCrystal).

1. Renseigner `WIFI_SSID` et `WIFI_PASS` dans le fichier avant de flasher (ne pas versionner le mot de passe).
2. Au démarrage, l'écran affiche l'adresse IP de l'ESP pendant quelques secondes : c'est la valeur à donner à `--esp`.
3. Le PC et l'ESP doivent être sur le même réseau. L'IP peut changer à chaque connexion (DHCP) : une réservation d'adresse dans le routeur est recommandée.

Les demandes du PC sont placées dans une file et traitées dans `loop()` : le callback réseau ne doit jamais attendre la carte capteurs.

Protocole MQTT de l'ESP :

| Sens | Topic | Contenu |
|---|---|---|
| PC vers ESP | `esp/requetes/<temp\|dist\|hum\|gaz>` | ignoré |
| ESP vers PC | `esp/response/<capteur>` | `{"result":"<valeur>"}` |
| PC vers ESP | `esp/function/screen` | `{"str":"<16 car.><16 car.>"}` (`{"str":""}` efface l'écran sans bip) |
| PC vers ESP | `esp/function/granted`, `esp/function/denied` | mélodie accès autorisé ou refusé |
| ESP vers PC | `esp/presence` | `{"result":"<cm>"}` quand la distance est inférieure à 50 cm |

Diagnostic de la liaison : `python backend/pont_esp.py --esp <IP> --test` (affiche les messages reçus et envoie un texte de test à l'écran). `python backend/pont_esp.py --chercher` scanne le réseau pour retrouver l'ESP. Détails : `iot/PATCH_ESP_ECRAN_BUZZER.md`.

L'écran fait 16 colonnes sur 2 lignes et n'affiche pas les accents : les textes sont convertis en ASCII et raccourcis.

## Scénarios

Le moteur de scénarios (`backend/scenarios.py`) combine présence, identité, mesures et analyses pour décider de l'écran et du buzzer. Chaque événement est consigné dans le journal du tableau de bord.

- Accès : présence détectée, identification en cours, accès autorisé, visage inconnu (intrusion), présence sans visage exploitable, confiance insuffisante.
- Environnement : anomalie (niveau 1, bip intermittent) ou danger critique (niveau 2, alarme continue) pour le gaz, la fumée, la température, l'humidité et les dérives.
- Météo : voir plus bas.

Il n'y a plus de capteur infrarouge ni de RFID : la règle anti-usurpation par chaleur (ACC-05) est inactive. Le catalogue complet et les seuils sont dans `rapport_sentinel_x.md` et `backend/MQTT_SCHEMA.md`.

## Intelligence artificielle

### Reconnaissance faciale

`backend/vision.py` charge `ai/V2.pkcls` (réseau Orange, classes Caroline, Florent, Killian, Personne, Vide) et publie l'identité sur `sentinel/vision`. Prérequis : NumPy 2 ou plus, Orange3 3.40, scikit-learn, Orange3-ImageAnalytics. L'embedder doit être celui de l'entraînement (`FACE_EMBEDDER`, `inception-v3` par défaut) ; il passe par le serveur d'embedding d'Orange et demande Internet.

### Analyse des variations des mesures

`backend/analytics.py` relit les 5 dernières minutes en base (température, humidité, gaz), calcule les moyennes par minute et fait juger les variations par le modèle `ai/modele_variations.pkcls` (0 normal, 1 anomalie, 2 critique). Sans modèle, une règle simple sur `var_max` s'applique. Des limites de sécurité absolues restent actives dans tous les cas. Résultat : `GET /api/analytics`. Les limites se règlent dans `CONFIG` en haut du fichier.

Entraînement : `cd ai; python generer_dataset_variations.py; python entrainer_variations.py`. Les données d'entraînement sont simulées : elles doivent être remplacées par de vraies mesures étiquetées.

### Prévisions météo

Onglet Prévisions du tableau de bord : risque météo sur 72 h pour un lieu au choix (Paris par défaut, modifiable par un administrateur).

- Source : API Open-Meteo, gratuite et sans clé.
- Vigilance : seuils sur les prévisions (rafales 60 et 90 km/h, pluie 20 et 40 mm en 6 h, chaleur 35 et 40 °C, froid -5 et -15 °C).
- IA : au premier affichage d'un lieu, un modèle scikit-learn s'entraîne sur 4 ans d'historique (1 à 2 minutes) et estime la probabilité d'un événement notable dans les 24 h. Sa fiabilité (AUC sur données non vues) est affichée. Le modèle et le lieu sont mis en cache dans `backend/meteo_cache/` (non versionné).
- Journal : un événement `MET-01` (vigilance) ou `MET-02` (danger) est ajouté quand le niveau monte.
- Alarme : un danger prévu dans les 24 prochaines heures (`METEO_ALARME_H`) déclenche écran et buzzer comme une alerte de capteur. L'IA seule ne sonne qu'à partir de 60 % de probabilité.

Outil pédagogique : ce n'est pas une alerte officielle.

## Authentification et comptes

Le tableau de bord exige une connexion en deux étapes, contrôlée par le backend (toutes les routes `/api/*` sauf `/api/auth/*` et `/api/health` demandent un jeton de session) :

1. Visage : la webcam du PC s'allume ; une identification d'un membre actif, avec un seul visage et une confiance d'au moins 85 % (`AUTH_CONFIANCE_MIN`), suffit. `AUTH_IDENTIFICATIONS=3` en exige trois successives.
2. Mot de passe de la personne reconnue : haché en scrypt avec sel, 5 essais puis verrouillage de 60 s, doublé à chaque récidive.

Le jeton expire après 30 minutes d'inactivité (8 heures au maximum).

Deux caméras sont utilisées : celle de la porte (`vision.py`, allumée par la présence détectée ou par le bouton du tableau de bord) et la webcam du poste (`vision_poste.py`, allumée seulement pendant une connexion). Leurs canaux MQTT sont distincts (`sentinel/vision` et `sentinel/poste/*`).

### Administration des comptes

L'onglet Administration (réservé aux administrateurs) permet de créer des comptes (identifiant, nom affiché, mot de passe, rôle administrateur), de les activer ou désactiver et de changer un mot de passe.

- Tant qu'aucun administrateur n'existe, toute personne connectée accède à l'onglet : créer d'abord un compte administrateur. Le mode `--dev` y donne toujours accès.
- On ne peut ni se désactiver soi-même ni retirer le dernier administrateur actif.
- La connexion par visage ne fonctionne que si l'identifiant correspond à une classe du modèle de reconnaissance (Caroline, Florent, Killian). Les autres comptes se connectent par mot de passe tant que le modèle n'est pas réentraîné.
- Ligne de commande : `python backend/manage_users.py mdp <Prénom>` et `python backend/manage_users.py liste`.

## Limites connues

- Pas de détection de vivacité : une photo présentée à la webcam du poste peut valider l'étape visage, le mot de passe reste la protection.
- Le broker Mosquitto est en accès anonyme et sans TLS ; l'API et le tableau de bord sont en HTTP (acceptable en réseau local).
- Les identifiants de la base de données figurent dans `infra/docker-compose.yml` et `backend/db.py`.
- L'IA des variations est entraînée sur des données simulées.
- L'adresse IP de l'ESP est dynamique.
- La validation de bout en bout avec le matériel (présence, caméra, écran, buzzer) est en cours.
