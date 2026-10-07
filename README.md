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

## 🚀 Démarrage rapide (un seul terminal)

Prérequis : **Python 3.10+** (testé en 3.14), **Docker Desktop lancé**, Internet (l'embedder Inception v3 d'Orange).

```bash
git clone https://github.com/Caro-2105/SENTINEL-X.git
cd SENTINEL-X
python -m pip install -r backend/requirements.txt
python lancer.py            # sous Windows : double-clic sur lancer.bat
```

Au **premier lancement**, `lancer.py` vérifie les bibliothèques et les modèles, démarre Docker (base + broker),
liste les caméras du PC pour que vous choisissiez celle de la **porte** (USB externe) et celle du **poste**
(webcam intégrée), puis demande de **choisir votre prénom et un mot de passe** (8 caractères minimum).
Il ouvre ensuite http://127.0.0.1:8000/ : visage, puis mot de passe, puis tableau de bord. `Ctrl+C` arrête tout.

| Commande | Effet |
|---|---|
| `python lancer.py --simulateur` | joue un scénario sans capteurs ni caméra de porte (`--simulateur gaz`, `incendie`…) |
| `python lancer.py --sans-porte` | pas de caméra du boîtier (tester seulement la connexion) |
| `python lancer.py --reconfigurer` | re-choisir les caméras |
| `python lancer.py --mot-de-passe` | changer son mot de passe |
| `python lancer.py --verbeux` | afficher tous les logs techniques |

Dépannage : Docker non démarré (lancer Docker Desktop) ; mauvaise caméra (`--reconfigurer`, les numéros diffèrent d'un PC à l'autre) ;
port 8000, 1883 ou 5433 déjà utilisé (fermer l'ancien processus) ; variables d'environnement PowerShell : `$env:NOM="valeur"` (pas `set`).
Tests : `cd backend && python -m unittest discover -s tests -v`. Détail des stories et des topics : `backend/MQTT_SCHEMA.md`.

### Brancher l'IA
*   **Reconnaissance faciale** : `backend/vision.py` charge `ai/V2.pkcls` (réseau Orange, classes Caroline / Florent / Killian / Personne / Vide) et publie le résultat sur `sentinel/vision`. Lancer `python vision.py`. Prérequis : NumPy >= 2 (le modèle a été sauvegardé avec NumPy 2), Orange3 3.40, scikit-learn 1.5.2, Orange3-ImageAnalytics. L'embedder doit être celui du workflow d'entraînement (`FACE_EMBEDDER`, `inception-v3` par défaut) ; Inception v3 passe par le serveur d'embedding d'Orange et demande Internet.
*   **IA analytique (variations sur 5 min)** : `backend/analytics.py` relit en base les 5 dernières minutes (température, humidité, gaz), calcule les moyennes par minute et fait juger les variations par un modèle Orange `ai/modele_variations.pkcls` (0 normal / 1 anomalie / 2 critique). Les limites min/max se règlent dans `CONFIG` (haut de `analytics.py`). Sans modèle : règle simple `var_max`. Il détecte aussi les dérives lentes (pente sur 60 s + significativité statistique), et garde des limites de sécurité absolues indépendantes de `CONFIG`. Résultat : `GET /api/analytics`. Entraînement : `cd ai; python generer_dataset_variations.py; python entrainer_variations.py` (ou sous Orange : File `dataset_variations.tab` → Random Forest → Test & Score → Save Model sous `modele_variations.pkcls`). Les données d'entraînement sont SIMULÉES : à remplacer par de vraies mesures étiquetées.

## Authentification du tableau de bord (visage puis mot de passe)

Deux caméras, deux rôles (voir le « lore » : un boîtier à la porte, un PC à l'intérieur) :

| | Boîtier à la porte | Poste (PC du tableau de bord) |
|---|---|---|
| Caméra | caméra USB externe du boîtier | provisoirement la même caméra (la webcam intégrée reconnaît mal : qualité différente de l'entraînement) |
| Script | `python vision.py` | `python vision_poste.py` |
| Allumée par | détecteur de présence (PIR) ou bouton du front | une connexion au tableau de bord |
| Sert à | scénarios d'accès (LED verte, écran, alarme) | authentifier la personne devant le PC |
| Canaux MQTT | `sentinel/vision`, `sentinel/camera/*` | `sentinel/poste/vision`, `sentinel/poste/*` |

La page `frontend/index.html` s'ouvre sur une mire de connexion en deux étapes, **contrôlée par le backend** (toutes les routes `/api/*` sauf `/api/auth/*` exigent un jeton de session) :

1. **Visage** : la webcam du PC s'allume (aperçu affiché) ; **une** identification d'un membre actif (un seul visage, confiance ≥ 85 %, réglable par `AUTH_CONFIANCE_MIN`) suffit. `AUTH_IDENTIFICATIONS=3` exige 3 identifications successives (plus strict).
2. **Mot de passe** de la personne reconnue (haché en scrypt avec sel, 5 essais puis verrouillage 60 s, doublé à chaque récidive).

Le jeton expire après 30 min d'inactivité (8 h maximum). Le flux de chaque caméra n'est lisible qu'avec une clé tirée au lancement du script ; l'aperçu de la mire de connexion peut être retiré avec `AUTH_APERCU_LOGIN=0`.

Première utilisation (aucun mot de passe n'est dans le code) :

```
cd backend
python manage_users.py mdp Caroline     # idem Florent, Killian (nom = label du modèle Orange)
python manage_users.py liste
python main.py                          # écoute sur 127.0.0.1 ; API_HOST=0.0.0.0 pour l'ouvrir au réseau
python vision.py --choisir              # (une fois par PC) montre chaque caméra : Entrée sur la caméra externe USB de la porte
python vision_poste.py --choisir        # idem pour l'authentification (mémorisé dans backend/camera_config.json)
python vision_poste.py                  # caméra du poste (authentification)
python vision.py                        # caméra du boîtier (porte) : scénarios d'accès. Tant que le poste partage la même caméra, ne pas le lancer pendant une connexion
```

Limites connues, à traiter avant la livraison : pas de détection de « vivant » sur la webcam du poste (une photo peut valider l'étape 1 : le mot de passe reste la vraie barrière), broker Mosquitto en accès anonyme (n'importe qui sur le réseau peut publier une fausse identité sur `sentinel/poste/vision`), pas de HTTPS entre le navigateur et l'API (acceptable en local), identifiants de la base encore dans `docker-compose.yml` et `db.py`.
Tests : `python -m unittest discover -s tests -v`.
