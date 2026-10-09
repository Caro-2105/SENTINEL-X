# SENTINEL-X
## Rapport de Projet — Workshop Bac+4

> **Avant-poste Industriel du Futur**  
> Prototype cyber-physique de surveillance pour AetherCorp

---

## Table des matières

1. [Contexte & Mise en situation](#1-contexte--mise-en-situation)
2. [Vision initiale du projet](#2-vision-initiale-du-projet)
3. [Équipe & organisation](#3-équipe--organisation)
4. [Architecture mise en place](#4-architecture-mise-en-place)
5. [Composants matériels](#5-composants-matériels)
6. [Détails techniques par couche](#6-détails-techniques-par-couche)
7. [Les "Stories" : scénarios d'alerte](#7-les-stories--scénarios-dalerte)
8. [Intelligence Artificielle](#8-intelligence-artificielle) (dont 8.4 IA prédictive météo)
9. [Difficultés rencontrées](#9-difficultés-rencontrées)
10. [État d'avancement et limites](#10-état-davancement-et-limites)
11. [Conclusion](#11-conclusion)

> [!NOTE]
> Ce rapport décrit le projet **tel qu'il fonctionne aujourd'hui**. Quand le prototype s'est éloigné de la vision initiale (capteurs abandonnés, firmware différent, fonctions non finies), c'est écrit explicitement, avec la raison et l'impact.

---

## 1. Contexte & Mise en situation

Dans le cadre du workshop Bac+4 organisé par notre formation, nous avons été plongés dans la peau d'ingénieurs mandatés par la société fictive **AetherCorp** pour concevoir et développer un **Avant-Poste Industriel du Futur**.

La mission, intitulée **MISSION SENTINEL-X**, consistait à concevoir un **prototype cyber-physique complet** capable de :

- **Surveiller** un site industriel en temps réel grâce à une batterie de capteurs environnementaux.
- **Détecter** intelligemment les intrusions physiques en distinguant le personnel autorisé des inconnus.
- **Alerter** automatiquement et de manière contextuelle en cas d'anomalie détectée (fuite de gaz, surchauffe, fumée, intrusion).
- **Communiquer** entre le boîtier embarqué (le "Edge Node") et un Centre de Commandement Tactique local (PC Serveur).

**Mise en situation retenue.** Le boîtier à capteurs est installé **à l'entrée** d'un local : il détecte l'arrivée d'une personne, l'identifie par caméra et affiche sur son petit écran si elle est autorisée ou non. Les personnes déjà à l'intérieur utilisent le **PC serveur** pour consulter le tableau de bord (courbes et analyses). Ce PC possède donc **sa propre webcam**, utilisée pour authentifier la personne qui s'y connecte, indépendamment de la caméra de la porte.

> [!NOTE]
> Le cahier des charges interdit les simples seuils codés en dur (ex : `if temperature > 40`) : la détection d'anomalies doit reposer sur un **modèle de Machine Learning**. Notre solution combine un modèle Orange et des garde-fous statistiques et de sécurité ; ses limites (données d'entraînement simulées, seuils de sécurité conservés) sont détaillées en sections 8.2 et 10.

---

## 2. Vision initiale du projet

Dès les premières sessions, l'équipe a produit un ensemble de **notes et d'idées** qui ont guidé nos choix techniques. Ces idées allaient au-delà des exigences minimales du sujet.

### Ce que le sujet demandait (minimum)
- Capteurs : PIR, MQ-2, DHT, Webcam.
- Actionneurs : Buzzer, LED, Écran OLED.
- Communication : MQTT entre l'ESP8266 et un serveur.
- Dashboard : Courbes en temps réel.

### Ce que nous avons voulu aller chercher en plus, et ce qu'il en est devenu
| Idée | Justification | Statut final |
|------|---------------|--------------|
| **Capteur RFID** | Authentification locale par badge | ❌ Abandonné : absent du firmware et du tableau de bord (la colonne `rfid_uid` reste en base, inutilisée) |
| **Reconnaissance faciale (Cas "Terminator")** | Distinguer les membres de l'équipe des intrus | ✅ Réalisé (modèle Orange `ai/V2.pkcls` + détecteur de visage YuNet) |
| **Capteur infrarouge (thermique)** | Détecter la chaleur humaine pour contrer la photo présentée à la caméra | ❌ Non retenu dans le montage final, remplacé par un capteur de distance : la règle anti-usurpation existe dans le code mais **ne peut plus se déclencher** |
| **Analyse de logs par IA** | Détecter les cyberattaques par les anomalies réseau | ❌ Non réalisé |
| **Architecture "Full Python"** | Cohérence avec le modèle IA généré via Orange | ✅ Réalisé |
| **Authentification du dashboard (visage + mot de passe)** | Sécuriser l'accès aux données | ✅ Ajouté en cours de projet |
| **Lanceur unique** (`lancer.py`) | Démarrer tout le projet en une commande sur n'importe quel PC | ✅ Ajouté en cours de projet |

---

## 3. Équipe & organisation

L'équipe s'est scindée en deux pôles complémentaires dès le départ :

```
┌─────────────────────────────────┐    ┌──────────────────────────────────┐
│    PÔLE PROTOTYPE (Hardware)    │    │      PÔLE BACK (Infra / Dev)     │
│                                 │    │                                  │
│ • Firmware C++ ESP8266          │    │ • Serveur Python (FastAPI)       │
│ • Câblage des capteurs          │    │ • Base de données PostgreSQL     │
│ • Boîtier & montage             │    │ • IA Analytics (Orange + Python) │
│ • Lecture des capteurs (série)  │    │ • IA Reconnaissance faciale      │
│                                 │    │ • Pont ESP ↔ Serveur             │
│                                 │    │ • Dashboard Frontend             │
└─────────────────────────────────┘    └──────────────────────────────────┘
```

Cette organisation a permis un travail en parallèle efficace : le pôle back pouvait développer et tester son code (simulateur de capteurs, scénarios rejouables) sans attendre que le câblage soit terminé. L'interface entre les deux pôles est le protocole MQTT du firmware, décrit en section 6.4.

---

## 4. Architecture mise en place

L'architecture repose sur **trois ensembles** qui communiquent par MQTT : le boîtier, le PC serveur et les caméras.

```
        BOÎTIER (porte)                              PC SERVEUR
┌──────────────────────────────┐    Wi-Fi    ┌─────────────────────────────────────────────┐
│ Carte capteurs               │             │  [pont_esp.py]                              │
│ (température, humidité,      │             │    │ interroge l'ESP, reformate             │
│  gaz, distance)              │             │    ▼                                        │
│        │ liaison série       │◄───────────►│  [Mosquitto] ◄──► [Backend FastAPI] ──► [PostgreSQL]
│        ▼                     │  requêtes   │                        │  ▲                 │
│ ESP8266 (mini-broker MQTT)   │  réponses   │                        │  └─ [IA Orange .pkcls]
│  • écran LCD 16x2            │  ordres     │                        ▼                    │
│  • haut-parleur              │             │      [Dashboard Chart.js] (servi par le back)│
└──────────────────────────────┘             └─────────────────────────────────────────────┘
 Caméra USB (porte) ─► [vision.py] ──────────► sentinel/vision ─┘
 Webcam du PC ───────► [vision_poste.py] ────► sentinel/poste/* (uniquement pendant une connexion)
```

### Flux de données (de bout en bout)
1. Le **pont** (`pont_esp.py`) interroge l'ESP toutes les 2 secondes (`esp/requetes/temp`, `dist`, `hum`, `gaz`). L'ESP relaie chaque demande à la carte capteurs par liaison série et renvoie la valeur sur `esp/response/<capteur>`.
2. Le pont déduit la **présence** (distance inférieure à 60 cm, maintenue 3 s) et publie un message JSON unique sur `sentinel/sensors` (broker Mosquitto du PC).
3. Le **backend** (`main.py`) consomme ces données, les enregistre en base et les soumet à l'**IA analytique** (relecture des 5 dernières minutes).
4. `vision.py` pilote la **caméra de la porte** : elle ne s'allume que si une présence est détectée (ou sur demande depuis le dashboard, 5 minutes maximum). Il interroge le **modèle de reconnaissance faciale** et publie le résultat sur `sentinel/vision`.
5. Le **moteur de scénarios** combine capteurs, vision et résultat IA et publie une **commande** (niveau de buzzer + 2 lignes d'écran) sur `sentinel/commandes`, à chaque changement et toutes les 2 secondes.
6. Le pont **traduit** cette commande en fonctions du firmware (`esp/function/screen`, `granted`, `denied`) : texte sur l'écran du boîtier et sons.
7. Le **dashboard** interroge l'API : état en direct toutes les secondes, courbes toutes les 2 s, événements toutes les 3 s.
8. Pour ouvrir le dashboard, `vision_poste.py` allume la **webcam du PC** le temps de la connexion (sur le topic séparé `sentinel/poste/*`, pour que les passages devant la porte n'influencent jamais une connexion).

---

## 5. Composants matériels

Voici l'inventaire du prototype tel qu'il est branché aujourd'hui :

| Composant | Rôle | Remarques |
|-----------|------|-----------|
| **NodeMCU ESP8266** | Passerelle Wi-Fi du boîtier, mini-broker MQTT, pilote l'écran et le haut-parleur | Liaison série (9600 bauds) avec la carte capteurs |
| **Carte capteurs** | Mesure et répond aux commandes série `TEMP`, `HUM`, `GAZ`, `DIST` | Seule la liste des commandes est vue côté PC |
| **Capteur température / humidité** | Ambiance du local | DHT (DHT11 d'après `MQTT_SCHEMA.md`) |
| **Capteur MQ-2** | Gaz combustibles et fumée | Valeur brute (~200 au repos) ; **préchauffage de quelques minutes** à chaque démarrage à froid |
| **Capteur de distance** | Détecte la présence d'une personne (joue le rôle du PIR) | Seuil de présence : 60 cm (réglable) |
| **Écran LCD 1602 (16 × 2)** | Messages d'état et d'alerte | Mode 4 bits parallèle (broches D1, D2, D5 à D8) ; pas d'accents |
| **Haut-parleur / buzzer** | Bips et mélodies (accès accordé / refusé) | Broche D0 ; pas d'alarme continue |
| **Caméra USB externe** | Caméra de la **porte** (reconnaissance des personnes qui arrivent) | Branchée sur le PC serveur |
| **Webcam intégrée du PC** | Authentification de la personne qui ouvre le dashboard | Branchée sur le PC serveur |

**Prévus au départ mais absents du montage final** : détecteur PIR, capteur infrarouge thermique, module RFID RC522, LED d'état, écran OLED.

> [!NOTE]
> Le firmware de l'ESP ne publie pas spontanément ses mesures et n'a pas de notion de « niveau de buzzer ». C'est le pont, côté PC, qui adapte le protocole du back à ce que le firmware sait faire (voir 6.4 et 7).

---

## 6. Détails techniques par couche

### 6.1 Infrastructure (Docker)
**Fichier :** `infra/docker-compose.yml`

Le serveur est orchestré via **Docker Compose**, ce qui garantit une installation reproductible sur n'importe quelle machine. Deux conteneurs sont déployés :

- **Mosquitto 2** (port 1883) : le broker MQTT du PC. Il relie le pont, le backend et les scripts de vision. Il est configuré en accès anonyme.
- **PostgreSQL 15** (port 5433) : la base relationnelle qui stocke l'historique des mesures, les détections de visages, les événements d'alerte et les membres autorisés. Le port n'est exposé que sur le PC lui-même (`127.0.0.1:5433`).

> [!TIP]
> Le port PostgreSQL a été changé de 5432 (standard) à **5433** pour éviter les conflits avec un PostgreSQL Windows potentiellement installé localement sur le PC. Le code le gère via la variable d'environnement `DB_PORT`.

> [!NOTE]
> L'ESP ayant son propre mini-broker, il ne se connecte **pas** à Mosquitto : c'est le pont qui se connecte aux deux. Le port 1883 de Mosquitto n'a donc plus besoin d'être ouvert au réseau.

### 6.2 Backend Python (Cœur du système)
**Fichier principal :** `backend/main.py`

Le backend est une application **FastAPI** qui assume plusieurs responsabilités en parallèle (multithreading) :

- **API REST** : `/api/status` (état en direct), `/api/data/history` (courbes), `/api/events` (journal, acquittement), `/api/camera` (commande de la caméra de la porte), `/api/analytics` (analyse IA), `/api/auth/*` (connexion), `/api/health` (santé, publique).
- **Service du tableau de bord** : le front est servi par le backend à l'adresse `http://127.0.0.1:8000/` (redirigée vers `/app/`), sans second serveur.
- **Prévisions météo** : `meteo_risque.py` interroge l'API gratuite Open-Meteo (sans clé), calcule une vigilance par seuils et une probabilité d'événement donnée par un modèle d'IA (voir 8.4). Résultats mis en cache 15 minutes, rafraîchis par un thread de fond qui alimente aussi l'alarme de l'ESP. Routes : `GET /api/meteo`, `POST /api/meteo/lieu` (admin).
- **Administration des comptes** : `GET/POST/PATCH /api/admin/membres` (réservées aux administrateurs) pour créer, activer, désactiver, promouvoir un compte et changer un mot de passe.
- **Client MQTT** : écoute `sentinel/sensors`, `sentinel/vision`, `sentinel/camera/state`, `sentinel/poste/vision` et `sentinel/poste/state`.
- **Moteur de scénarios** : à chaque message reçu, l'état est réévalué et une commande est publiée pour le boîtier.
- **Session sécurisée** : toutes les routes de données exigent un jeton obtenu après la connexion en deux étapes (visage puis mot de passe, voir 8.3).

Le backend ne fait pas lui-même la reconnaissance faciale : celle-ci est réalisée par `vision.py` (porte) et `vision_poste.py` (poste), qui publient leurs résultats sur MQTT.

### 6.3 Base de données (Schéma MERISE)
**Fichier :** `backend/db.py`

Le schéma relationnel comporte 5 tables :

```
sensor_data          membre               detection_visage
───────────          ──────               ────────────────
id (PK)              id_membre (PK)       id_detection (PK)
timestamp            label_ia (UNIQUE)    horodatage
temperature          nom_affiche          label_ia
humidite             rfid_uid (inutilisé) confiance
gaz                  actif                connu
presence             cree_le              id_membre (FK)
rfid_uid (inutilisé) mot_de_passe_hash
ir_temp (inutilisé)                       evenement
                                          ─────────
scenario                                  id_evenement (PK)
────────                                  horodatage
code_scenario (PK)                        code_scenario (FK)
categorie                                 id_mesure (FK)
libelle                                   id_detection (FK)
severite                                  categorie_env
buzzer                                    score_ia
ligne1_ecran                              modele_ia
ligne2_ecran                              message_ecran
                                          instantane (JSONB)
                                          acquitte
```

Un **pool de connexions** (10 connexions max, avec attente quand il est plein et vérification des connexions avant usage) évite d'ouvrir une connexion à chaque message ; sans lui, le tableau de bord saccadait.

> [!NOTE]
> Les horodatages sont stockés sans fuseau horaire. L'API les renvoie **avec** fuseau, sinon le navigateur les lisait en heure locale et les courbes affichaient 2 heures de décalage (voir section 9). Les colonnes `rfid_uid` et `ir_temp` n'ont plus de capteur associé. Le schéma Merise doit encore être mis à jour avec `membre.mot_de_passe_hash` et `membre.admin` (rôle administrateur, `BOOLEAN`). Le catalogue `scenario` contient en plus `MET-01` (vigilance météo) et `MET-02` (danger météo).

### 6.4 Le Pont ESP
**Fichiers :** `backend/pont_esp.py`, `backend/ordres_esp.py`

> [!IMPORTANT]
> C'est un composant clé qui a émergé d'une contrainte matérielle majeure.

Le firmware de l'ESP8266 retenu par le pôle Hardware fait tourner un **mini-broker MQTT interne** (uMQTTBroker). Il ne publie pas ses mesures : il répond à des demandes, et n'accepte que quelques connexions à la fois.

**Protocole du firmware**

| Sens | Topic | Contenu | Effet |
|------|-------|---------|-------|
| PC → ESP | `esp/requetes/<capteur>` (`temp`, `dist`, `hum`, `gaz`) | ignoré | l'ESP envoie `TEMP`/`DIST`/`HUM`/`GAZ` à la carte capteurs |
| ESP → PC | `esp/response/<capteur>` | `{"result":"24.4"}` | valeur lue |
| PC → ESP | `esp/function/screen` | `{"str":"<16 car.><16 car.>"}` | écran effacé puis écrit, bip court |
| PC → ESP | `esp/function/granted` / `denied` | vide | mélodie accès accordé / refusé |
| ESP → PC | `esp/presence` | `{"result":"<cm>"}` | publié de lui-même sous 50 cm (non utilisé par le pont) |

**Rôle du pont (`pont_esp.py`)**
1. **Interroger l'ESP** toutes les 2 secondes sur les capteurs choisis (`--esp-capteurs temp,dist,hum,gaz`) et signaler les réponses manquantes, avec un bilan toutes les 30 secondes.
2. **Reformater** les réponses en JSON pour le backend (`sentinel/sensors`) et **déduire la présence** de la distance (seuil 60 cm réglable, maintien de 3 s pour éviter les clignotements).
3. **Convertir** la température si le firmware la renvoie en °F (`--esp-fahrenheit`).
4. **Relayer** les ordres du backend vers l'ESP via `ordres_esp.py` (voir ci-dessous).
5. **Diagnostiquer** : `--chercher` (retrouve l'ESP sur le réseau si son adresse a changé), `--test` (affiche tout ce que l'ESP renvoie).
6. **Ménager l'ESP** : identifiant de connexion fixe (une connexion fantôme est remplacée au lieu de saturer le mini-broker) et déconnexion propre à l'arrêt.

**Traduction des ordres (`ordres_esp.py`, module pur et testé)**
Le firmware n'a que des fonctions ponctuelles, sans alarme continue ni niveaux de buzzer. Le pont adapte donc le contrat du back :
- l'écran n'est réécrit qu'à chaque **changement d'état** (chaque écriture vide l'écran et fait un bip) ; l'état de repos « SENTINEL-X / Surveillance OK » devient un écran vide et silencieux ;
- accès autorisé : texte puis mélodie `granted` ;
- niveaux de buzzer 1 et 2 : mélodie `denied` **répétée** tant que la situation dure (toutes les 10 s en anomalie, 3 s en danger) ;
- les textes sont passés en ASCII et tiennent sur 16 colonnes (« Bienvenue Caroline » devient « Caroline », « CONFIANCE TROP FAIBLE » devient « CONFIANCE FAIBLE »).

### 6.5 Frontend Dashboard
**Fichiers :** `frontend/index.html`, `frontend/app.js`

Interface web sombre (thème industriel), servie par le backend. Cinq onglets (le dernier n'est visible que des administrateurs) :

- **Capteurs** : tuiles en direct (température, humidité, détecteur de présence, gaz, analyse IA) et courbes **Chart.js** (température + humidité, gaz) sur les 90 dernières secondes, aux échelles qui s'adaptent automatiquement.
- **Caméra porte** : la caméra n'est active que si une présence est détectée ; un bouton permet de la forcer (extinction automatique après 5 minutes). Le flux vidéo est protégé par une clé aléatoire.
- **Événements** : journal des alertes avec acquittement par le superviseur.
- **Prévisions** : niveau de risque météo sur 72 h, conditions actuelles, courbe de probabilité de l'IA avec les rafales, liste des dangers prévus (type, niveau, heure, valeur) et, pour un administrateur, changement de lieu (ville).
- **Administration** : création de comptes (identifiant, nom affiché, mot de passe, case « Administrateur »), activation / désactivation, rôle admin, changement de mot de passe. Garde-fous : on ne peut ni se désactiver soi-même ni retirer le dernier administrateur actif. Tant qu'aucun administrateur n'existe, toute personne connectée y accède (bandeau d'avertissement) ; le mode `--dev` y donne toujours accès.

L'écran de connexion fait passer par la reconnaissance faciale (webcam du PC) puis le mot de passe. Les rafraîchissements sont séquentiels (état 1 s, courbes 2 s, événements 3 s) et n'agissent que quand une nouvelle mesure arrive, ce qui supprime les à-coups.

### 6.6 Lancement, outillage et tests

- **`lancer.py`** (ou double-clic sur `lancer.bat`) remplace les cinq terminaux. Au premier lancement il vérifie les bibliothèques et les modèles, démarre Docker, fait choisir la caméra de la porte et celle du poste (mémorisées par PC dans `camera_config.json`) et fait créer le mot de passe du membre. Il démarre ensuite le backend, la caméra du poste, la caméra de la porte et, si demandé, le pont de l'ESP, puis ouvre le navigateur. `Ctrl+C` arrête l'ensemble.
- **Options** : `--esp <IP>` (avec `--esp-capteurs`, `--esp-seuil`, `--esp-fahrenheit`), `--simulateur [scénario]` (joue un scénario sans matériel), `--sans-porte`, `--sans-env` (n'agit que sur les scénarios d'accès, coupe aussi l'alarme météo), `--sans-meteo` (désactive les prévisions, aucun appel à Internet), `--reconfigurer`, `--mot-de-passe`, `--dev`, `--verbeux`.
- **Mode développeur** (`--dev`) : bouton d'entrée sans visage ni mot de passe, accepté uniquement depuis le PC lui-même et uniquement si le serveur a été lancé avec ce mode. **À ne jamais utiliser pour une démonstration ou une livraison.**
- **Tests** : 94 tests unitaires sans matériel (moteur de scénarios 20, IA analytique 22, authentification et identifiants de compte 25, traduction des ordres vers l'ESP 7, prévisions météo et alarme 20).
- **Gestion des mots de passe** : `backend/manage_users.py` (`liste`, `mdp <Prénom>`).

---

## 7. Les "Stories" : scénarios d'alerte

**Fichier :** `backend/scenarios.py`

L'un des modules les plus riches du projet. Le **moteur de scénarios** (`MoteurScenarios`) est un composant **pur** (zéro entrée/sortie) qui reçoit l'état courant des capteurs + vision + IA et renvoie une `Decision` (texte d'écran, niveau de buzzer, événements à journaliser).

### Catalogue complet des scénarios

#### Scénarios d'accès (présence + caméra)

| Code | Déclencheur | Buzzer | Message d'écran |
|------|-------------|--------|-----------------|
| **IDLE** | Aucune présence | 0 | `SENTINEL-X / Surveillance OK` (écran laissé vide sur le boîtier) |
| **ACC-03** | Présence détectée, caméra en cours d'analyse (jusqu'à 8 s) | 0 | `IDENTIFICATION / Regardez la camera` |
| **ACC-01** | Membre reconnu (confiance ≥ 80 %) | 0 🟢 | `ACCES AUTORISE / Bienvenue {nom}` (maintenu 8 s) |
| **ACC-06** | Membre reconnu mais confiance trop faible (< 80 %) | 1 ⚠️ | `CONFIANCE FAIBLE / Taux : {xx}%` |
| **ACC-02** | Visage inconnu | 2 🔴 | `INTRUSION / Personne inconnue` |
| **ACC-04** | Présence sans visage exploitable après 8 s | 1 ⚠️ | `ALERTE PRESENCE / Non identifie` |
| **ACC-05** | Visage connu **sans** chaleur humaine (photo) | 2 🔴 | `ACCES REFUSE / Chaleur absente` — **inactif sur le prototype actuel, faute de capteur infrarouge** |

> [!WARNING]
> **Sans capteur infrarouge, rien ne distingue une photo d'un membre d'un vrai visage** : le moteur accepte alors le visage reconnu. La règle anti-usurpation (ACC-05) est écrite, testée et s'activerait dès qu'un capteur de chaleur serait branché, mais elle n'est pas opérationnelle aujourd'hui. C'est une limite de sécurité connue (pas de détection de vivacité non plus).

#### Scénarios environnementaux (analyse IA des séries temporelles)

| Code | Déclencheur | Buzzer | Message d'écran |
|------|-------------|--------|-----------------|
| **ENV-01** | Anomalie environnementale détectée (niveau 1) | 1 ⚠️ | `ATTENTION / {type}` |
| **ENV-02** | Danger environnemental critique (niveau 2) | 2 🔴 | `ALERTE DANGER / {type}` |

Les types affichés sont : `Fuite de gaz`, `Fumee / incendie`, `Surchauffe`, `Humidite anormal`, `Derive suspecte`. Ces réactions sont **actives par défaut** (désactivables avec `--sans-env`).

**Réglages actuels** (`backend/analytics.py`, à ajuster avec les vraies mesures)

| Mesure | Anomalie (niveau 1) | Critique (niveau 2) | Variation max entre deux minutes |
|--------|---------------------|---------------------|----------------------------------|
| Température | ≥ 35 °C ou ≤ 10 °C | ≥ 40 °C ou ≤ 0 °C | 3 °C |
| Humidité | ≥ 65 % ou ≤ 35 % | ≥ 80 % ou ≤ 30 % | 10 % |
| Gaz | ≥ 400 | ≥ 600 | 10 % |

Les valeurs de gaz sont provisoires. Des filets de sécurité absolus s'ajoutent (gaz ≥ 400 / 600, température ≥ 40 / 50 °C, humidité ≤ 10 % ou ≥ 95 %).

**Retour à la normale.** Seule la variation de la **dernière minute** déclenche l'alerte de variation, et l'IA n'est écoutée que si une mesure a varié d'au moins 5 % sur cette minute. Sans cela, un pic restait « critique » jusqu'à 5 minutes après son retour à la normale. Les limites absolues, elles, restent actives sans délai.

#### Scénarios météo (prévisions Open-Meteo)

| Code | Déclencheur | Réaction |
|------|-------------|----------|
| **MET-01** | Vigilance : un seuil de niveau 1 est prévu dans les 72 h | Consigné dans le journal, pas d'alarme à lui seul |
| **MET-02** | Danger : un seuil de niveau 2 est prévu dans les 72 h | Consigné dans le journal, pas d'alarme à lui seul |

**Seuils (niveau 1 / niveau 2)** : rafales 60 / 90 km/h, pluie 20 / 40 mm cumulés sur 6 h, chaleur 35 / 40 °C, froid -5 / -15 °C.

**Alarme météo.** Si un danger est prévu dans les **24 prochaines heures** (réglable avec `METEO_ALARME_H`), il emprunte le même chemin que les alertes de capteurs (ENV-01 / ENV-02) : niveau 1 = bip intermittent et `ATTENTION / {type}`, niveau 2 = alarme continue et `ALERTE DANGER / {type}`. Types affichés : `Vent violent`, `Fortes pluies`, `Forte chaleur`, `Grand froid`, `Risque meteo IA`. L'IA seule ne déclenche l'alarme qu'à partir de **60 %** de probabilité. Un danger plus lointain reste affiché et consigné, mais le buzzer ne sonne pas pendant des jours. Si un capteur réel est déjà en alerte, le niveau le plus élevé l'emporte. Un résultat de plus de 3 heures (API injoignable) ne déclenche plus d'alarme.

> [!NOTE]
> Les messages sont limités à **16 caractères par ligne** (écran LCD 1602) et sans accents.

---

## 8. Intelligence Artificielle

Trois briques d'IA ou d'analyse répondent à trois besoins distincts : identifier les personnes à la porte, authentifier l'utilisateur du dashboard, et juger l'environnement.

### 8.1 IA de Reconnaissance Faciale
**Fichiers :** `backend/vision.py`, `backend/vision_poste.py`, `ai/V2.pkcls`, `ai/face_detection_yunet_2023mar.onnx`

- **Outil** : **Orange Data Mining** (extension Image Analytics). Le modèle `ai/V2.pkcls` reconnaît cinq classes : `Caroline`, `Florent`, `Killian`, `Personne` et `Vide`.
- **Chaîne de traitement** : une image de la caméra → détection du visage (**YuNet**, OpenCV, modèle de 230 Ko versionné dans le dépôt) → vecteur d'embedding **Inception v3** → modèle Orange. Sans visage détecté, le résultat est « Vide ». Une classe inconnue du modèle donne « inconnu ».
- **Prérequis techniques** : NumPy ≥ 2 (le modèle a été sauvegardé avec NumPy 2) et **accès Internet** (Inception v3 passe par le serveur d'embedding d'Orange).
- **Deux scripts de vision, deux caméras, deux canaux MQTT** :
  - `vision.py` : caméra de la **porte** (USB externe), pilotée par la présence. Elle s'éteint sans présence et peut être forcée depuis le dashboard (5 minutes maximum). Elle publie sur `sentinel/vision`.
  - `vision_poste.py` : webcam du **PC**, allumée uniquement pendant une connexion au dashboard. Elle publie sur `sentinel/poste/vision`.
- **Partage de caméra** : si les deux scripts utilisent la même caméra physique (PC avec une seule caméra), la caméra de la porte se met en pause pendant une connexion.
- **Choix des caméras par PC** : les numéros de caméra diffèrent d'une machine à l'autre ; ils sont choisis au premier lancement et mémorisés dans `camera_config.json` (non versionné).

> [!WARNING]
> La qualité du flux compte : avec la caméra intégrée du PC, le modèle a rendu « inconnu » avec 100 % de confiance, probablement parce que ses images diffèrent de celles de l'entraînement. L'authentification fonctionne avec la caméra externe ; la caméra intégrée peut demander de ré-entraîner le modèle avec des photos prises par elle.

### 8.2 IA Analytique Environnementale
**Fichier :** `backend/analytics.py`

C'est la réponse à l'exigence du sujet d'éviter les simples `if`. L'analyse porte sur les **5 dernières minutes** relues en base (moyennes par minute) et combine **4 niveaux** :

1. **Modèle de variations (Orange)** : un classifieur (`ai/modele_variations.pkcls`) classe chaque fenêtre de 5 minutes en `normal` / `anomalie` / `critique` à partir de quatre indicateurs de variation en % (dernière minute, maximum, écart-type, écart à la moyenne). Sans modèle ou en cas d'erreur, une règle simple (`var_max`) prend le relais.
2. **Limites par mesure** : pré-alerte avant la limite critique (tableau de la section 7).
3. **Dérive statistique** (régression linéaire + significativité de Student) : détecte une montée lente mais certaine sur une fenêtre glissante de 60 secondes, avant que les moyennes par minute ne bougent. Une hausse simultanée de température et de gaz est repérée comme indicateur d'incendie.
4. **Limites de sécurité absolues** : filet toujours actif (gaz ≥ 600, température ≥ 50 °C, etc.) qui ne dépend ni du modèle ni des réglages.

L'analyse produit un **niveau** (0 normal, 1 anomalie, 2 critique), un **score**, une **catégorie** (`gaz`, `incendie`, `surchauffe`, `humidite`, `derive`) et une **raison** lisible (visible en survolant le badge « Analyse IA » du dashboard).

> [!NOTE]
> **Limite assumée :** le modèle a été entraîné sur des séries **simulées** (`ai/generer_dataset_variations.py`) et non sur des mesures réelles du boîtier. Les seuils par mesure et les filets de sécurité sont, eux, des valeurs fixes : l'IA décide des variations, mais l'exigence « pas de seuils codés en dur » n'est respectée que partiellement. Ré-entraîner avec des enregistrements réels étiquetés est la suite logique.

### 8.3 Authentification du Dashboard
**Fichiers :** `backend/auth.py`, `backend/vision_poste.py`, `backend/manage_users.py`

Connexion en deux étapes, avec identification **sans saisie de nom** :
1. **Étape Visage** : la webcam du PC s'allume à la demande de connexion. Seuls les membres actifs reconnus avec une confiance ≥ **85 %** passent. Le résultat n'est valable que pour la connexion en cours (usage unique, 3 minutes pour se faire reconnaître, 2 minutes ensuite pour saisir le mot de passe).
2. **Étape Mot de passe** : hachage **scrypt** avec sel, 8 caractères minimum, **5 essais** par reconnaissance puis verrouillage exponentiel (60 s, doublé à chaque récidive, plafonné à 15 min).
3. **Session** : jeton aléatoire, expire après 30 minutes d'inactivité ou 8 heures au total. Les routes de données sont refusées sans jeton (401).

Limites : pas de détection de vivacité (une photo présentée à la webcam du PC passerait l'étape visage, le mot de passe restant la protection), et trafic en HTTP non chiffré (réseau local).

La création des comptes se fait depuis l'onglet **Administration** du tableau de bord (ou `manage_users.py`). La connexion par visage n'est possible que pour un identifiant égal à une classe du modèle de reconnaissance (Caroline, Florent, Killian) ; les autres comptes se connectent par mot de passe tant que le modèle n'est pas réentraîné.

### 8.4 IA prédictive météo
**Fichier :** `backend/meteo_risque.py`

**Objectif.** Estimer, pour un lieu donné, la probabilité qu'un événement météo notable survienne dans les 24 heures suivantes.

**Données.** L'API Open-Meteo fournit les prévisions sur 72 h (température, humidité, pression, nuages, vent, rafales, pluie) et un historique horaire de 4 ans issu de la réanalyse ERA5.

**Méthode.** Pour chaque heure de l'historique, on calcule l'état de l'atmosphère et ses tendances : chute de pression sur 3 h et 12 h, cumul de pluie sur 6 h et 24 h, rafales maximales, saison. L'étiquette vaut 1 si un seuil de vigilance est franchi dans les 24 h suivantes. Un modèle de gradient boosting (scikit-learn, `HistGradientBoostingClassifier`) apprend ce lien, puis il est appliqué à chaque heure de la prévision : on obtient une courbe de probabilité sur 72 h. L'entraînement se fait seul, en arrière-plan, au premier affichage d'un lieu (1 à 2 minutes) ; le modèle est conservé dans `backend/meteo_cache/` et réentraîné à chaque changement de lieu.

**Deux niveaux d'analyse complémentaires.** La *vigilance* compare les prévisions à des seuils fixes : explicable, elle donne toujours le « pourquoi ». L'*IA* estime une probabilité à partir de l'état et des tendances de l'atmosphère.

**Évaluation.** Le modèle est testé sur les 20 % les plus récents de l'historique, jamais vus à l'entraînement, avec 24 h d'écart pour éviter toute fuite d'information. La métrique est l'AUC (0,5 = hasard, 1 = parfait), affichée dans le tableau de bord. Les tests automatiques utilisent des données synthétiques : ils prouvent le fonctionnement du code, pas la qualité réelle du modèle.

> [!NOTE]
> **Limites assumées :** les étiquettes viennent de nos propres seuils, le modèle prédit donc leur franchissement et non un cataclysme au sens large. Les prévisions fournies en entrée sont déjà issues d'un modèle météo professionnel : notre IA est une couche statistique propre à un lieu, et son apport réel face à la prévision brute reste à démontrer. Au-dessous de 50 cas dans l'historique d'un lieu, le modèle refuse de s'entraîner et seule la vigilance par seuils reste active. La prédiction de séismes n'est pas traitée. Ce n'est pas un système d'alerte officiel.

---

## 9. Difficultés rencontrées

### 🔴 Le mini-broker de l'ESP (Principale difficulté)
La plus grande contrainte est venue du firmware retenu par le pôle Hardware. L'ESP8266 fait tourner **uMQTTBroker** : il ne publie pas ses mesures spontanément et fonctionne en **requête/réponse**, à l'opposé du modèle push-subscribe classique de MQTT. Il a fallu développer `pont_esp.py`, qui interroge l'ESP, reformate les réponses, déduit la présence et traduit les ordres du back en fonctions du firmware.

**Difficultés dans la durée :**
- Le mini-broker accepte **très peu de connexions** : des connexions fantômes (arrêts brutaux) le saturaient et il refusait alors toute nouvelle connexion. Corrigé par un identifiant de connexion fixe et un arrêt propre.
- Son **adresse IP change** à chaque redémarrage (DHCP) : d'où l'option `--chercher`.
- Les premières versions du protocole (`esp/request/...`) ont été remplacées par `esp/requetes/...` et `esp/function/...` : le pont s'est adapté.
- **En cours :** les demandes de capteurs lancées par un client ne reçoivent pas de réponse alors que la détection de présence interne de l'ESP fonctionne. Notre hypothèse est que le firmware attend la carte capteurs avec `yield()` à l'intérieur de la fonction appelée par le réseau, ce qui est interdit sur ESP8266 et provoque un redémarrage. Correctif proposé au pôle Hardware : mettre les demandes dans une file traitée par `loop()`.

### 🔴 Évolution du matériel en cours de route
Le capteur infrarouge, le RFID, la LED et l'écran OLED n'ont finalement pas été montés ; le PIR a été remplacé par un capteur de distance. Conséquences : la règle anti-photo (ACC-05) est devenue inactive, la présence se déduit d'un seuil de distance, l'écran est un LCD 16 × 2 aux textes raccourcis, et l'humidité et le gaz ne sont remontés qu'une fois les demandes `hum` et `gaz` ajoutées au firmware. Le code a été adapté sans casser les scénarios (les mesures absentes sont facultatives).

### 🔴 Caméras sous Windows
- Les **numéros de caméra** changent d'un PC à l'autre (la caméra intégrée peut être 0 ou 1) : le choix est désormais fait au premier lancement et mémorisé.
- Le pilote par défaut mettait **30 à 90 secondes** à ouvrir certaines caméras, ce qui faisait expirer la session de connexion : corrigé par un réglage d'OpenCV et un délai de connexion porté à 3 minutes.
- Les deux scripts de vision se disputaient la même caméra physique : arbitrage (la porte se met en pause pendant une connexion).
- Une connexion exécutée par erreur avec le résultat de la caméra de la porte était possible : les deux caméras ont des canaux MQTT séparés.

### 🟡 Un tableau de bord qui « saccade »
Rafraîchissements qui se chevauchaient, connexion à la base ouverte à chaque requête, verrou tenu pendant les accès base, échelles des courbes instables. Corrigé par le pool de connexions, une route d'état sans verrou, des échelles qui s'adaptent par paliers, des boucles séquentielles et un redessin seulement quand une nouvelle mesure arrive.

### 🟡 Courbes décalées de 2 heures
Les horodatages stockés sans fuseau étaient lus comme de l'heure locale par le navigateur. L'API renvoie désormais des horodatages avec fuseau.

### 🟡 Une alerte qui reste « critique » après le retour à la normale
La fenêtre d'analyse de 5 minutes gardait un pic en mémoire. Désormais seule la variation de la dernière minute compte (voir section 7).

### 🟡 Unités et valeurs aberrantes
La température a d'abord été remontée en °F (87 « °C » déclenchaient des alertes critiques) ; le pont a reçu une option de conversion, devenue inutile depuis la correction côté capteurs. Le MQ-2 demande un préchauffage qui fausse les premières minutes.

### 🟡 Conflits de port
PostgreSQL local sur 5432 (Docker passé sur 5433, avec port par défaut corrigé partout) et ports de flux caméra interdits par Windows (`WinError 10013`, repli automatique sur le port suivant).

### 🟡 L'écran n'affiche pas les accents
L'écran LCD ne gère ni accents ni caractères spéciaux, et le firmware coupe le texte au premier guillemet. Tout texte est normalisé en ASCII (`scenarios.py` puis `ordres_esp.py`) et raccourci à 16 caractères.

### 🟡 Prédire des événements rares
Les phénomènes extrêmes sont peu fréquents dans l'historique d'un seul lieu, ce qui rend l'entraînement fragile. Nous avons choisi des événements « notables » (niveau vigilance jaune) pour avoir assez d'exemples. Nous avons aussi séparé les règles, qui font sonner l'alarme à elles seules, de l'IA, qui ne sonne qu'à forte probabilité, pour limiter les fausses alarmes.

### 🟡 Anti-doublon dans le journal d'événements
Sans protection, chaque message (toutes les 2 secondes) pouvait générer un événement. Un **cooldown de 30 secondes** limite un même scénario pour un même sujet.

### 🟡 Synchronisation caméra / capteur infrarouge (historique)
Quand le capteur IR existait, sa mesure et l'image n'arrivaient pas ensemble. Le moteur applique un TTL de 5 s sur la vision et exige une mesure IR de moins d'1 s ; cette protection reste dans le code, mais n'a plus d'effet sans IR.

---

## 10. État d'avancement et limites

| Élément | Statut | Notes |
|---------|--------|-------|
| Reconnaissance faciale (`ai/V2.pkcls` + YuNet) | ✅ Fonctionnelle | Bonne avec la caméra externe ; dégradée avec la caméra intégrée du PC |
| IA des variations (`modele_variations.pkcls`) | ⚠️ Présente, données simulées | À ré-entraîner avec des mesures réelles étiquetées |
| Authentification du dashboard | ✅ Fonctionnelle | Visage + mot de passe, 23 tests ; pas de détection de vivacité, HTTP non chiffré |
| Lecture des capteurs (température, humidité, gaz, distance) | ✅ Validée sur le matériel | Via le pont ; un cycle prend quelques secondes |
| Réponse du firmware aux demandes de capteurs | ⏳ Correctif demandé au pôle Hardware | File d'attente à traiter dans `loop()` |
| Écran et sons du boîtier | ⏳ À valider sur le matériel | Traduction testée ; dépend du firmware ci-dessus |
| Scénarios d'accès bout en bout (présence, caméra, message, buzzer) | ⏳ Tests en cours | Logique testée sans matériel ; essais réels après correction du firmware |
| Scénarios environnementaux | ✅ Testés (simulation et mesures) | Seuils à affiner |
| Règle anti-usurpation par IR (ACC-05) | ❌ Inactive | Aucun capteur de chaleur monté |
| Détection de vivacité (liveness) | ❌ Non réalisée | |
| Interface d'administration des comptes | ✅ Développée | Création, activation, rôle admin, mot de passe ; testée sans la vraie base ; visage possible seulement pour les personnes connues du modèle |
| IA prédictive météo + vigilance | ✅ Développée | Open-Meteo ; AUC réelle à mesurer sur le lieu choisi (tests faits sur données synthétiques) |
| Alarme météo (écran + buzzer) | ⏳ À valider sur le matériel | Logique testée ; dépend du firmware de l'ESP |
| Tests unitaires | ✅ 94 tests | Scénarios 20, analytique 22, authentification 25, ordres vers l'ESP 7, météo 20 |
| Lanceur unique + README de démarrage | ✅ Développé | `lancer.py` ; pas encore éprouvé sur tous les PC de l'équipe |
| Archivage automatique (`archiver.py`) | ✅ Développé | Rotation CSV + purge PostgreSQL ; non revalidé récemment |
| Adresse IP de l'ESP | ⚠️ Dynamique | Prévoir une réservation d'adresse dans le routeur |
| Sécurisation | ⏳ Partielle | Mosquitto anonyme, pas de TLS/HTTPS, identifiants de base de données en clair dans `docker-compose.yml` et `db.py` (à retirer de l'archive livrée) |
| Mode développeur (`--dev`) | ⚠️ À ne pas utiliser en démo | Contourne l'authentification (PC local seulement) |
| Schéma Merise | ⏳ À mettre à jour | Ajouter `membre.mot_de_passe_hash` et `membre.admin`, retirer RFID / IR |
| Vidéo de démonstration | ⏳ À tourner | Quand le boîtier sera validé de bout en bout |

---

## 11. Conclusion

Le projet SENTINEL-X a réuni en une seule réalisation de nombreuses compétences de la formation :

- **IoT & Embarqué** : dialogue bidirectionnel avec un boîtier à mini-broker MQTT, via un pont qui adapte les protocoles.
- **Infrastructure** : orchestration Docker, broker MQTT, base relationnelle, lanceur reproductible sur chaque PC.
- **Intelligence Artificielle** : reconnaissance faciale (Orange + détecteur de visage) et analyse temporelle des mesures combinant modèle, statistiques et garde-fous.
- **Développement Backend** : API REST FastAPI, pool de connexions, multithreading, sessions sécurisées, 71 tests automatiques.
- **Cybersécurité** : authentification en deux facteurs (visage + mot de passe scrypt), verrouillage progressif, routes protégées. La protection anti-photo par infrarouge, prévue, n'est pas active sur le prototype final.
- **Frontend** : tableau de bord de supervision avec courbes dynamiques, caméra pilotée et journal d'événements.

Le projet a surtout appris à **s'adapter à un matériel qui évolue** : capteurs remplacés, firmware au protocole inattendu, caméras différentes d'un PC à l'autre. Les choix d'architecture (moteur de scénarios pur, mesures facultatives, pont qui traduit les protocoles, canaux MQTT séparés) ont permis d'absorber ces changements sans réécrire le cœur. Les limites restantes sont connues et listées en section 10 : la validation bout en bout sur le boîtier, l'entraînement sur des données réelles et la sécurisation d'un déploiement réel.

---

*Document mis à jour le 8 octobre 2026 — Projet SENTINEL-X, Workshop Bac+4*
