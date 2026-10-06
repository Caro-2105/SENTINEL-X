"""
SENTINEL-X : reconnaissance des membres de l'équipe par la webcam.

Chaîne :
    image de la webcam  ->  embedding (2048 valeurs)  ->  réseau de neurones Orange  ->  5 classes
    image de la webcam  ->  YuNet (OpenCV)            ->  nombre de visages

Classes du modèle : Caroline, Florent, Killian (équipe), Personne (inconnu), Vide (personne devant la caméra).
Le modèle Orange classe l'image ENTIÈRE ; YuNet sert à compter les visages et à sécuriser la décision.

Résultat publié sur `sentinel/vision` (contrat dans MQTT_SCHEMA.md) environ toutes les PERIODE_ANALYSE_S.
La caméra n'est ALLUMÉE que si le détecteur de présence (PIR, topic `sentinel/sensors`) est actif, ou si
l'utilisateur la force depuis l'onglet « Caméra » du front (topic `sentinel/camera/cmd`). Sinon elle est
libérée (voyant éteint). L'image est servie en MJPEG sur http://localhost:8001/stream (accessible uniquement
depuis ce PC) et l'état est publié sur `sentinel/camera/state`.
Prérequis : NumPy >= 2, Orange3, Orange3-ImageAnalytics, PyQt5, opencv-python >= 4.5.4,
            et le fichier face_detection_yunet_2023mar.onnx (OpenCV Zoo : opencv/opencv_zoo,
            models/face_detection_yunet), à placer dans ai/.
"""
import json
import os
import pickle
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from Orange.data import Table, Domain

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MODEL_PATH = os.getenv("FACE_MODEL_PATH", os.path.join(BASE_DIR, "..", "ai", "V2.pkcls"))
DETECTOR_PATH = os.getenv("FACE_DETECTOR_PATH",
                          os.path.join(BASE_DIR, "..", "ai", "face_detection_yunet_2023mar.onnx"))
# Embedder utilisé dans Orange pour entraîner le modèle. 2048 valeurs => "inception-v3" (ou "painters").
# ATTENTION : inception-v3 passe par le serveur api.garaza.io (connexion Internet, images envoyées).
EMBEDDER = os.getenv("FACE_EMBEDDER", "inception-v3")

CLASSE_VIDE = "Vide"          # scène sans personne
CLASSE_INCONNU = "Personne"   # personne présente mais pas dans l'équipe

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_VISION = "sentinel/vision"
PERIODE_ANALYSE_S = 1.0
TAILLE_IMAGE = (720, 480)     # redimensionnement systématique avant analyse
SEUIL_VISAGE = 0.7            # score minimal de détection YuNet

CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "1"))        # 0 = webcam par défaut, 1 = seconde caméra
FLUX_PORT = int(os.getenv("CAMERA_STREAM_PORT", "8001"))
FLUX_FPS = 15                 # cadence du flux envoyé au front
AFFICHER_FENETRE = os.getenv("FACE_WINDOW", "0") == "1"   # fenêtre OpenCV en plus du front
MQTT_TOPIC_CAPTEURS = "sentinel/sensors/#"
MQTT_TOPIC_CAMERA_ETAT = "sentinel/camera/state"
MQTT_TOPIC_CAMERA_CMD = "sentinel/camera/cmd"
PIR_MAINTIEN_S = 10.0         # la caméra reste allumée 10 s après la dernière détection de présence
MANUEL_MAX_S = 300.0          # un allumage forcé depuis le front s'éteint seul au bout de 5 min


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


def load_face_detector():
    """Charge le détecteur de visages YuNet (ne mémorise aucun visage, il ne fait que compter)."""
    if not os.path.isfile(DETECTOR_PATH):
        print(f"❌ Détecteur YuNet introuvable à {DETECTOR_PATH} "
              f"(télécharger face_detection_yunet_2023mar.onnx depuis opencv/opencv_zoo).")
        return None
    try:
        detecteur = cv2.FaceDetectorYN.create(DETECTOR_PATH, "", TAILLE_IMAGE, score_threshold=SEUIL_VISAGE)
        print("✅ Détecteur de visages YuNet chargé")
        return detecteur
    except Exception as e:
        print(f"❌ Chargement de YuNet impossible ({e}). Vérifier : opencv-python >= 4.5.4.")
        return None


def compter_visages(detecteur, frame):
    """Nombre de visages détectés dans la trame."""
    h, w = frame.shape[:2]
    detecteur.setInputSize((w, h))
    _, visages = detecteur.detect(frame)
    return 0 if visages is None else len(visages)


def predire(model, embedding):
    """Probabilités par classe pour un embedding (le modèle applique lui-même sa normalisation)."""
    attributs = model.original_domain.attributes
    X = np.asarray(embedding, dtype=float).reshape(1, -1)
    if X.shape[1] != len(attributs):
        raise ValueError(f"embedding de {X.shape[1]} valeurs, le modèle en attend {len(attributs)} "
                         f"(FACE_EMBEDDER={EMBEDDER} ne correspond pas à celui d'entraînement)")
    table = Table.from_numpy(Domain(attributs), X)
    return model(table, model.Probs)[0]


def interpreter(classes, probs, nb_visages):
    """Classe prédite + nombre de visages -> (label, confiance, connu, visages) pour `sentinel/vision`.

    Fail-closed : un membre n'est "connu" que si le modèle le reconnaît ET qu'exactement
    un visage est détecté (0 = photo/objet/profil douteux, >=2 = impossible de savoir qui est qui).
    """
    i = int(np.argmax(probs))
    label, confiance = classes[i], float(probs[i])
    if label == CLASSE_VIDE:
        return "Vide", confiance, False, nb_visages
    if label == CLASSE_INCONNU:
        return "inconnu", confiance, False, max(nb_visages, 1)
    connu = (nb_visages == 1)
    return label, confiance, connu, nb_visages


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


def boucle_analyse(partage, model, embedder, detecteur, client, arret):
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
            nb_visages = compter_visages(detecteur, frame)
            p = publish_vision(client, *interpreter(classes, probs, nb_visages))
            print(f"🧑 Vision -> {p}")
        except Exception as e:
            # Fail-closed : rien n'est publié, le backend considère vite la vision périmée (5 s)
            # et une présence non identifiée finit en alerte ACC-04, jamais en autorisation.
            print(f"❌ Analyse impossible : {e}")


class Controle:
    """Décide si la caméra doit être allumée : présence (PIR) récente OU allumage forcé depuis le front."""

    def __init__(self):
        self.lock = threading.Lock()
        self.presence_ts = 0.0       # dernière trame capteurs avec presence == 1
        self.manuel_depuis = None    # date de l'allumage forcé, None = mode automatique

    def presence(self, now):
        with self.lock:
            self.presence_ts = now

    def commande(self, action, now):
        with self.lock:
            self.manuel_depuis = now if action == "on" else None

    def etat(self, now):
        """(allumée ?, mode) avec mode = "manuel" ou "auto"."""
        with self.lock:
            if self.manuel_depuis is not None and now - self.manuel_depuis < MANUEL_MAX_S:
                return True, "manuel"
            self.manuel_depuis = None
            return now - self.presence_ts < PIR_MAINTIEN_S, "auto"


class Flux:
    """Dernière image JPEG, partagée entre la boucle caméra et le serveur HTTP."""

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg = None
        self.version = 0

    def mettre(self, jpeg):
        with self.cond:
            self.jpeg = jpeg
            self.version += 1
            self.cond.notify_all()

    def effacer(self):
        self.mettre(None)


def demarrer_serveur_flux(flux):
    """Sert l'image en MJPEG sur 127.0.0.1 (jamais exposée au réseau : c'est une caméra)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            chemin = self.path.split("?")[0]
            if chemin == "/snapshot.jpg":
                with flux.cond:
                    jpeg = flux.jpeg
                if jpeg is None:
                    self.send_error(503, "camera eteinte")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(jpeg)
            elif chemin == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                vue = -1
                try:
                    while True:
                        with flux.cond:
                            flux.cond.wait_for(lambda: flux.version != vue, timeout=2.0)
                            vue, jpeg = flux.version, flux.jpeg
                        if jpeg is None:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
            else:
                self.send_error(404)

    # Sous Windows certains ports sont réservés (Hyper-V, WSL, Docker : WinError 10013) : on essaie les suivants.
    for port in range(FLUX_PORT, FLUX_PORT + 20):
        try:
            serveur = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            print(f"⚠️ Port {port} indisponible, essai du suivant…")
    else:
        raise RuntimeError(f"aucun port libre entre {FLUX_PORT} et {FLUX_PORT + 19} (définir CAMERA_STREAM_PORT)")
    serveur.daemon_threads = True
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    print(f"🌐 Flux caméra : http://127.0.0.1:{port}/stream (local uniquement)")
    return port


def start_camera_auth():
    """Pilote la caméra (allumée seulement si présence ou ordre du front), identifie et publie sur MQTT."""
    model = load_orange_model()
    if model is None:
        return
    detecteur = load_face_detector()
    if detecteur is None:
        return
    # Import tardif : orangecontrib.imageanalytics charge Qt (PyQt5 requis)
    from orangecontrib.imageanalytics.image_embedder import ImageEmbedder
    embedder = ImageEmbedder(model=EMBEDDER)

    ctl, flux = Controle(), Flux()

    def on_connect(client, userdata, flags, rc):
        client.subscribe(MQTT_TOPIC_CAPTEURS)
        client.subscribe(MQTT_TOPIC_CAMERA_CMD)

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
            if msg.topic == MQTT_TOPIC_CAMERA_CMD:
                if data.get("action") in ("on", "auto"):
                    ctl.commande(data["action"], time.time())
                    print(f"🎛️  Commande caméra : {data['action']}")
            elif data.get("presence") == 1:
                ctl.presence(time.time())
        except Exception:
            pass

    client = mqtt.Client("SentinelX-Vision")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception as e:
        print(f"❌ Broker MQTT injoignable ({e}) : ni présence ni commandes ne seront reçues.")

    port_flux = demarrer_serveur_flux(flux)

    partage = {"frame": None, "lock": threading.Lock()}
    arret = threading.Event()
    threading.Thread(target=boucle_analyse,
                     args=(partage, model, embedder, detecteur, client, arret),
                     daemon=True).start()

    cap, dernier_jpeg = None, 0.0
    etat_cam = {"actif": False, "erreur": None}

    def publier_etat():
        # Thread dédié : ouvrir/lire la webcam peut bloquer la boucle principale plusieurs secondes,
        # et l'absence d'état ferait passer le front en "service hors ligne".
        while not arret.is_set():
            _, mode = ctl.etat(time.time())
            client.publish(MQTT_TOPIC_CAMERA_ETAT, json.dumps({
                "actif": etat_cam["actif"], "mode": mode, "erreur": etat_cam["erreur"],
                "flux": f"http://127.0.0.1:{port_flux}/stream"}))
            arret.wait(1.0)

    threading.Thread(target=publier_etat, daemon=True).start()
    print("⏸️  Caméra en veille : elle s'allume sur détection de présence ou depuis le front. Ctrl+C pour quitter.")
    try:
        while True:
            now = time.time()
            voulue, mode = ctl.etat(now)

            if voulue and cap is None:
                cap = cv2.VideoCapture(CAMERA_INDEX)
                if cap.isOpened():
                    etat_cam.update(actif=True, erreur=None)
                    print("🎥 Caméra allumée")
                else:
                    cap.release()
                    cap = None
                    etat_cam.update(actif=False, erreur="Impossible d'ouvrir la webcam")
                    print(f"❌ Impossible d'ouvrir la webcam (index {CAMERA_INDEX}, voir CAMERA_INDEX)")
                    time.sleep(2)
            elif not voulue and cap is not None:
                cap.release()
                cap = None
                etat_cam["actif"] = False
                flux.effacer()
                with partage["lock"]:
                    partage["frame"] = None
                if AFFICHER_FENETRE:
                    cv2.destroyAllWindows()
                print("⏸️  Caméra éteinte (plus de présence)")

            if cap is not None:
                ret, frame = cap.read()
                if not ret:
                    print("❌ Erreur de lecture de la caméra.")
                    cap.release()
                    cap = None
                    etat_cam.update(actif=False, erreur="Erreur de lecture de la caméra")
                    time.sleep(1)
                    continue
                frame = cv2.resize(frame, TAILLE_IMAGE)
                with partage["lock"]:
                    partage["frame"] = frame
                if now - dernier_jpeg >= 1.0 / FLUX_FPS:     # ~15 images/s suffisent et allègent le PC
                    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok:
                        flux.mettre(jpeg.tobytes())
                    dernier_jpeg = now
                if AFFICHER_FENETRE:
                    cv2.imshow("SENTINEL-X : Authentification Faciale", frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
            else:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        arret.set()
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()
        client.publish(MQTT_TOPIC_CAMERA_ETAT, json.dumps({"actif": False, "mode": "auto", "erreur": "arrêtée"}))
        client.loop_stop()


if __name__ == "__main__":
    start_camera_auth()
