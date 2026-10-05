# SENTINEL-X

Projet d'avant-poste industriel du futur développé dans le cadre du Workshop Bac+4.

## 🎯 Objectif
Créer un prototype cyber-physique (Edge Node) autonome capable de détecter les menaces environnementales, d'intrusions physiques, et cyber-sécuritaires pour AetherCorp.

## 🏗️ Architecture et Technologies

L'équipe s'est orientée vers un écosystème **Full Python** pour la partie serveur :

*   **IA & Data** : Modèles de Machine Learning générés via **Orange** et exportés au format `.pkl`.
*   **Backend** : Serveur Python gérant la logique métier, l'ingestion MQTT et l'utilisation des modèles IA `.pkl`.
*   **IoT & Edge** : ESP8266 (Firmware C/C++) avec capteurs de gaz (MQ-2), présence (PIR), température/humidité (DHT22), RFID (pour l'authentification locale), reconnaissance faciale et infrarouge (Cas "Terminator").
*   **Infra & Cyber** : Docker-compose (Serveur MQTT Mosquitto), sécurité et chiffrement des flux.

## 📂 Arborescence du Projet

*   `/iot` : Code source C++ pour le microcontrôleur ESP8266 et gestion des capteurs matériels.
*   `/ai` : Stockage des modèles entraînés (ex: `modele_orange.pkl`) et scripts d'inférence Python (vision, analyse de séries temporelles / logs).
*   `/backend` : Application Python (gestionnaire MQTT, appels à l'IA, logique d'alerte).
*   `/infra` : Fichiers de configuration de l'infrastructure (ex: `docker-compose.yml`, configs Mosquitto).

## 🚀 Démarrage rapide

```bash
cd infra && docker compose up -d                 # Mosquitto + PostgreSQL
cd ../backend && pip install -r requirements.txt
python main.py                                    # API + moteur de scénarios (http://localhost:8000)
python simulator.py demo                          # rejoue toutes les stories SANS matériel
python -m unittest discover -s tests -v           # tests des stories (sans broker ni base)
```

Ouvrir `frontend/index.html` pour le dashboard. Détail des stories et des topics : `backend/MQTT_SCHEMA.md`.

### Brancher l'IA
*   **Reconnaissance faciale** : `backend/vision.py` charge `ai/V1.pkcls` (réseau Orange, classes Caroline / Florent / Killian / Personne / Vide) et publie le résultat sur `sentinel/vision`. Lancer `python vision.py`. Prérequis : NumPy >= 2 (le modèle a été sauvegardé avec NumPy 2), Orange3 3.40, scikit-learn 1.5.2, Orange3-ImageAnalytics. L'embedder doit être celui du workflow d'entraînement (`FACE_EMBEDDER`, `inception-v3` par défaut) ; Inception v3 passe par le serveur d'embedding d'Orange et demande Internet.
*   **IA environnement** : déposer `ai/modele_environnement.pkl` (contrat dans l'en-tête de `backend/env_ai.py`). Sans fichier, une baseline statistique + des limites de sécurité sont utilisées.
