"""
SENTINEL-X : caméra du POSTE (webcam intégrée du PC) pour authentifier la personne qui ouvre le tableau de bord.

Deux caméras, deux rôles :
  - boîtier à la porte  -> vision.py        : pilotée par le détecteur de présence, alimente les scénarios d'accès
  - webcam du PC        -> vision_poste.py  : allumée uniquement pendant une connexion au tableau de bord

Même modèle de reconnaissance (ai/V2.pkcls), mais canaux séparés :
    sentinel/poste/cmd     backend -> ce script   {"action": "on"|"off"}   (« on » = bail de BAIL_S secondes)
    sentinel/poste/vision  ce script -> backend   {"label", "confiance", "connu", "visages"}
    sentinel/poste/state   ce script -> backend   {"actif", "erreur", "flux"}
Le flux (MJPEG, 127.0.0.1, protégé par une clé) ne sert qu'à l'aperçu sur la mire de connexion.

Réglage de la caméra (une fois par PC) : `python vision_poste.py --choisir` (montre chaque caméra, Entrée pour valider ;
mémorisé dans camera_config.json). Aussi : `--liste`, `--camera N [--enregistrer]`. POSTE_STREAM_PORT (8101) : port de l'aperçu.
ATTENTION : tant que le poste utilise la même caméra que le boîtier, ne pas avoir les deux ouvertes en même temps
(vision.py n'ouvre la sienne que sur présence ou bouton du front ; arrêter vision.py pendant les tests de connexion).
"""
import argparse
import json
import os
import secrets
import threading
import time

import cv2
import paho.mqtt.client as mqtt

import cameras
import vision as v          # réutilise le modèle, le détecteur de visages, l'embedder et le serveur de flux

# Caméra du POSTE : variable POSTE_CAMERA_INDEX > camera_config.json > même caméra que la porte (provisoire : la
# webcam intégrée reconnaît mal, la caméra d'entraînement est meilleure). Réglage : python vision_poste.py --choisir
POSTE_CAMERA_INDEX = cameras.index("poste", defaut=v.CAMERA_INDEX, env="POSTE_CAMERA_INDEX")
POSTE_FLUX_PORT = int(os.getenv("POSTE_STREAM_PORT", "8101"))
TOPIC_CMD = "sentinel/poste/cmd"
TOPIC_VISION = "sentinel/poste/vision"
TOPIC_ETAT = "sentinel/poste/state"
BAIL_S = 180.0               # la caméra s'éteint seule si le backend ne renouvelle pas la demande
FLUX_FPS = 15
PERIODE_ANALYSE_S = 0.3     # identifications enchaînées (le temps est surtout celui de l'embedding), pour une connexion rapide


def boucle_analyse(partage, model, embedder, detecteur, client, arret):
    classes = list(model.domain.class_var.values)
    while not arret.is_set():
        time.sleep(PERIODE_ANALYSE_S)
        with partage["lock"]:
            frame = None if partage["frame"] is None else partage["frame"].copy()
        if frame is None:
            continue
        try:
            probs = v.predire(model, v.embedder_frame(embedder, frame))
            label, confiance, connu, visages = v.interpreter(classes, probs, v.compter_visages(detecteur, frame))
            payload = {"label": label, "confiance": round(float(confiance), 3),
                       "connu": bool(connu), "visages": int(visages)}
            client.publish(TOPIC_VISION, json.dumps(payload))
            print(f"🧑 Poste -> {payload}")
        except Exception as e:
            # Fail-closed : rien n'est publié, l'étape 1 de la connexion ne peut pas être validée
            print(f"❌ Analyse impossible : {e}")


def main():
    global POSTE_CAMERA_INDEX
    ap = argparse.ArgumentParser(description="Caméra du poste pour l'authentification au tableau de bord")
    cameras.ajouter_options(ap)
    POSTE_CAMERA_INDEX, fini = cameras.traiter_options(ap.parse_args(), "poste", POSTE_CAMERA_INDEX)
    if fini:
        return
    print(f"📷 Caméra du poste : index {POSTE_CAMERA_INDEX}")
    model = v.load_orange_model()
    if model is None:
        return
    detecteur = v.load_face_detector()
    if detecteur is None:
        return
    from orangecontrib.imageanalytics.image_embedder import ImageEmbedder
    embedder = ImageEmbedder(model=v.EMBEDDER)

    bail = {"fin": 0.0}
    etat = {"actif": False, "erreur": None}

    def on_connect(client, userdata, flags, rc):
        client.subscribe(TOPIC_CMD)

    def on_message(client, userdata, msg):
        try:
            action = json.loads(msg.payload.decode()).get("action")
        except Exception:
            return
        if action == "on":
            if bail["fin"] < time.time():
                print("🎛️  Poste : demande de connexion -> caméra du PC")
            bail["fin"] = time.time() + BAIL_S
        elif action == "off":
            bail["fin"] = 0.0

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, "SentinelX-Poste")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(v.MQTT_BROKER, v.MQTT_PORT, 60)
        client.loop_start()
    except Exception as e:
        print(f"❌ Broker MQTT injoignable ({e}).")

    flux = v.Flux()
    cle = secrets.token_urlsafe(16)
    port = v.demarrer_serveur_flux(flux, cle, POSTE_FLUX_PORT)
    arret = threading.Event()
    partage = {"frame": None, "lock": threading.Lock()}
    threading.Thread(target=boucle_analyse, args=(partage, model, embedder, detecteur, client, arret),
                     daemon=True).start()

    def publier_etat():
        while not arret.is_set():
            client.publish(TOPIC_ETAT, json.dumps({"actif": etat["actif"], "erreur": etat["erreur"],
                                                   "flux": f"http://127.0.0.1:{port}/stream?k={cle}"}))
            arret.wait(1.0)

    threading.Thread(target=publier_etat, daemon=True).start()

    cap, dernier_jpeg = None, 0.0
    print("⏸️  Caméra du poste en veille : elle s'allume pendant une connexion au tableau de bord. Ctrl+C pour quitter.")
    try:
        while True:
            now = time.time()
            voulue = now < bail["fin"]
            if voulue and cap is None:
                cap = cameras.ouvrir_camera(POSTE_CAMERA_INDEX)
                limite = time.time() + 8          # la porte peut mettre un instant à libérer une caméra partagée
                while cap is None and time.time() < limite and time.time() < bail["fin"]:
                    time.sleep(0.5)
                    cap = cameras.ouvrir_camera(POSTE_CAMERA_INDEX)
                if cap is None:
                    etat.update(actif=False, erreur=f"Caméra {POSTE_CAMERA_INDEX} introuvable ou déjà utilisée (voir : python vision_poste.py --choisir)")
                    print(f"❌ {etat['erreur']}")
                    bail["fin"] = 0.0
                    time.sleep(1)
                    continue
                etat.update(actif=True, erreur=None)
                print("🎥 Caméra du poste allumée")
            elif not voulue and cap is not None:
                cap.release()
                cap = None
                etat["actif"] = False
                flux.effacer()
                with partage["lock"]:
                    partage["frame"] = None
                print("⏸️  Caméra du poste éteinte")

            if cap is None:
                time.sleep(0.2)
                continue
            ok, frame = cap.read()
            if not ok:
                print("❌ Erreur de lecture de la webcam du PC.")
                cap.release()
                cap = None
                etat.update(actif=False, erreur="Erreur de lecture de la webcam du PC")
                bail["fin"] = 0.0
                time.sleep(1)
                continue
            frame = cv2.resize(frame, v.TAILLE_IMAGE)
            with partage["lock"]:
                partage["frame"] = frame
            if now - dernier_jpeg >= 1.0 / FLUX_FPS:
                okj, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if okj:
                    flux.mettre(jpeg.tobytes())
                dernier_jpeg = now
    except KeyboardInterrupt:
        pass
    finally:
        arret.set()
        if cap is not None:
            cap.release()
        client.publish(TOPIC_ETAT, json.dumps({"actif": False, "erreur": "arrêtée", "flux": None}))
        client.loop_stop()


if __name__ == "__main__":
    main()
