"""
SENTINEL-X : reconnaissance des membres de l'équipe par la webcam.

Chaîne identique à celle du workflow Orange qui a servi à entraîner `ai/V1.pkcls` :
    image de la webcam  ->  embedding (2048 valeurs)  ->  réseau de neurones Orange  ->  5 classes
Classes du modèle : Caroline, Florent, Killian (équipe), Personne (inconnu), Vide (personne devant la caméra).
Le modèle classe l'image ENTIÈRE : pas de détection de visage préalable.

Résultat publié sur `sentinel/vision` (contrat dans MQTT_SCHEMA.md) environ toutes les PERIODE_ANALYSE_S.
Prérequis : NumPy >= 2 (le .pkcls a été sauvegardé avec NumPy 2), Orange3, Orange3-ImageAnalytics.
"""
import json
import os
import pickle
import tempfile
import threading
import time

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from Orange.data import Table, Domain

MODEL_PATH = os.getenv("FACE_MODEL_PATH",
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ai", "V1.pkcls"))
# Embedder utilisé dans Orange pour entraîner le modèle. 2048 valeurs => "inception-v3" (ou "painters").
# ATTENTION : inception-v3 passe par le serveur api.garaza.io (connexion Internet, images envoyées).
EMBEDDER = os.getenv("FACE_EMBEDDER", "inception-v3")

CLASSE_VIDE = "Vide"          # scène sans personne
CLASSE_INCONNU = "Personne"   # personne présente mais pas dans l'équipe

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_VISION = "sentinel/vision"
PERIODE_ANALYSE_S = 1.0
TAILLE_IMAGE = (640, 480)     # redimensionnement systématique avant analyse


def load_orange_model():
    """Charge le modèle de reconnaissance faciale généré par Orange"""
    try:
        with open(MODEL_PATH, 'rb') as f:
            model = pickle.load(f)
        print(f"✅ Modèle Orange chargé : classes = {list(model.domain.class_var.values)}")
        return model
    except FileNotFoundError:
        print(f"⚠️ Modèle introuvable à {MODEL_PATH}.")
    except Exception as e:  # ex. NumPy 1.x : "is not a known BitGenerator module"
        print(f"❌ Chargement du modèle impossible ({e}). Vérifier : NumPy >= 2, Orange3 3.40, scikit-learn 1.5.2.")
    return None


def predire(model, embedding):
    """Probabilités par classe pour un embedding (le modèle applique lui-même sa normalisation)."""
    attributs = model.original_domain.attributes
    X = np.asarray(embedding, dtype=float).reshape(1, -1)
    if X.shape[1] != len(attributs):
        raise ValueError(f"embedding de {X.shape[1]} valeurs, le modèle en attend {len(attributs)} "
                         f"(FACE_EMBEDDER={EMBEDDER} ne correspond pas à celui d'entraînement)")
    table = Table.from_numpy(Domain(attributs), X)
    return model(table, model.Probs)[0]


def interpreter(classes, probs):
    """Classe prédite -> (label, confiance, connu, visages) au format du topic `sentinel/vision`."""
    i = int(np.argmax(probs))
    label, confiance = classes[i], float(probs[i])
    if label == CLASSE_VIDE:
        return None, confiance, False, 0
    if label == CLASSE_INCONNU:
        return "inconnu", confiance, False, 1
    return label, confiance, True, 1


def embedder_frame(embedder, frame):
    """L'embedder Orange travaille sur des fichiers image : trame -> JPEG temporaire -> embedding."""
    fd, chemin = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        cv2.imwrite(chemin, frame)
        resultat = embedder([chemin])
    finally:
        os.remove(chemin)
    if resultat is None or len(resultat) == 0 or resultat[0] is None or len(resultat[0]) == 0:
        raise RuntimeError("embedding vide (serveur d'embedding injoignable ?)")
    return resultat[0]


def publish_vision(client, label, confiance, connu, visages):
    payload = {"label": label, "confiance": round(float(confiance), 3),
               "connu": bool(connu), "visages": int(visages)}
    client.publish(MQTT_TOPIC_VISION, json.dumps(payload))
    return payload


def boucle_analyse(partage, model, embedder, client, arret):
    """Thread d'analyse : l'appel à l'embedder peut durer ~1 s, il ne doit pas figer l'affichage vidéo."""
    classes = list(model.domain.class_var.values)
    while not arret.is_set():
        time.sleep(PERIODE_ANALYSE_S)
        with partage["lock"]:
            frame = None if partage["frame"] is None else partage["frame"].copy()
        if frame is None:
            continue
        try:
            probs = predire(model, embedder_frame(embedder, frame))
            p = publish_vision(client, *interpreter(classes, probs))
            print(f"🧑 Vision -> {p}")
        except Exception as e:
            # Fail-closed : rien n'est publié, le backend considère vite la vision périmée (5 s)
            # et une présence non identifiée finit en alerte ACC-04, jamais en autorisation.
            print(f"❌ Analyse impossible : {e}")


def start_camera_auth():
    """Démarre la webcam, identifie en continu et publie le résultat sur MQTT"""
    model = load_orange_model()
    if model is None:
        return
    # Import tardif : orangecontrib.imageanalytics charge Qt (présent avec l'installation d'Orange)
    from orangecontrib.imageanalytics.image_embedder import ImageEmbedder
    embedder = ImageEmbedder(model=EMBEDDER)

    client = mqtt.Client("SentinelX-Vision")
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception as e:
        print(f"❌ Broker MQTT injoignable ({e}) : les résultats ne seront pas publiés.")

    # Ouvre la webcam (l'ID 0 est généralement la webcam par défaut)
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("❌ Erreur : Impossible d'ouvrir la webcam.")
        return

    print("🎥 Webcam activée. Appuyez sur 'q' pour quitter.")
    partage = {"frame": None, "lock": threading.Lock()}
    arret = threading.Event()
    threading.Thread(target=boucle_analyse, args=(partage, model, embedder, client, arret), daemon=True).start()

    while True:
        ret, frame = cap.read()
        if not ret:
            print("❌ Erreur de lecture de la caméra.")
            break
        frame = cv2.resize(frame, TAILLE_IMAGE)
        with partage["lock"]:
            partage["frame"] = frame
        cv2.imshow("SENTINEL-X : Authentification Faciale", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    arret.set()
    cap.release()
    cv2.destroyAllWindows()
    client.loop_stop()


if __name__ == "__main__":
    start_camera_auth()
