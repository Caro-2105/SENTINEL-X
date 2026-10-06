from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Literal
import paho.mqtt.client as mqtt
import json
import threading
import time
from dataclasses import asdict
import uvicorn
import db
from scenarios import MoteurScenarios, Capteurs, Vision
from analytics import AnalyticsAI

app = FastAPI(title="SENTINEL-X API")

# Autoriser les requêtes CORS pour le Dashboard (Front-end local)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration MQTT ---
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "sentinel/sensors/#"          # ESP8266 -> serveur (capteurs)
MQTT_TOPIC_VISION = "sentinel/vision"      # script caméra -> serveur (identité)
MQTT_TOPIC_COMMANDES = "sentinel/commandes"  # serveur -> ESP8266 (LED verte, buzzer, OLED)
MQTT_TOPIC_CAMERA_ETAT = "sentinel/camera/state"  # vision.py -> serveur (caméra allumée ? mode ?)
MQTT_TOPIC_CAMERA_CMD = "sentinel/camera/cmd"      # serveur -> vision.py (forcer / revenir en auto)
HEARTBEAT_COMMANDE_S = 2.0                 # la commande est ré-émise même sans changement
VISION_PERSIST_S = 10.0                    # une détection identique n'est consignée qu'une fois / 10 s

# --- État partagé (un seul thread MQTT écrit ; l'API lit) ---
moteur = MoteurScenarios()
analytique = AnalyticsAI()
_lock = threading.Lock()
mqtt_client = None
etat = {"camera": None, "capteurs": None, "vision": None, "env": None, "decision": None,
        "id_mesure": None, "id_detection": None, "commande": None, "commande_ts": 0.0}
_membres_cache = {"data": {}, "ts": 0.0}
_derniere_detection = {"label": None, "connu": None, "ts": 0.0}


def _membres(force=False):
    if force or time.time() - _membres_cache["ts"] > 10:
        _membres_cache["data"] = db.load_membres()
        _membres_cache["ts"] = time.time()
    return _membres_cache["data"]


def _num(x):
    return None if x is None else float(x)


def evaluer_et_agir():
    """Rejoue le moteur de scénarios, commande les actionneurs de l'ESP et journalise."""
    now = time.time()
    membres = _membres()
    decision = moteur.evaluer(etat["capteurs"], etat["vision"], etat["env"], membres, now)
    etat["decision"] = decision

    # 1) Commande vers l'ESP8266 : sur changement, sinon en "heartbeat"
    commande = decision.commande()
    if commande != etat["commande"] or now - etat["commande_ts"] >= HEARTBEAT_COMMANDE_S:
        if mqtt_client is not None:
            mqtt_client.publish(MQTT_TOPIC_COMMANDES, json.dumps(commande))
        if commande != etat["commande"]:
            print(f"🎛️  {decision.scenarios} -> {commande}")
        etat["commande"], etat["commande_ts"] = commande, now

    # 2) Journal des événements (uniquement à l'apparition d'un scénario)
    for ev in decision.evenements:
        id_evt = db.insert_evenement(
            code_scenario=ev.code,
            message_ecran=f"{ev.ligne1} / {ev.ligne2}",
            id_mesure=etat["id_mesure"],
            # seuls les scénarios fondés sur un visage (ACC-01/02/05) pointent vers une détection
            id_detection=etat["id_detection"] if ev.code in ("ACC-01", "ACC-02", "ACC-05") else None,
            categorie_env=ev.categorie_env, score_ia=ev.score_ia, modele_ia=ev.modele_ia,
            instantane=ev.instantane)
        print(f"📝 Événement #{id_evt} {ev.code} : {ev.ligne1} / {ev.ligne2}")


def traiter_capteurs(data):
    id_mesure = db.insert_sensor_data(
        temperature=data.get("temperature"),
        humidite=data.get("humidite"),
        gaz=data.get("gaz"),
        presence=data.get("presence"),
        rfid_uid=data.get("rfid_uid"),
        ir_temp=data.get("ir_temp")
    )
    now = time.time()
    c = Capteurs(temperature=_num(data.get("temperature")), humidite=_num(data.get("humidite")),
                 gaz=_num(data.get("gaz")), presence=int(data.get("presence") or 0),
                 ir_temp=_num(data.get("ir_temp")), rfid_uid=data.get("rfid_uid"), ts=now)
    env = analytique.analyser(c, now)      # relit les 5 dernières minutes en base (la mesure vient d'être insérée)
    with _lock:
        etat["capteurs"], etat["id_mesure"] = c, id_mesure
        etat["env"] = env
        evaluer_et_agir()


def traiter_vision(data):
    label = data.get("label")
    now = time.time()
    v = Vision(label=label, confiance=float(data.get("confiance") or 0.0), connu=bool(data.get("connu")),
               visages=int(data.get("visages", 1 if label else 0)), ts=now)
    with _lock:
        membres = _membres()
        if v.visages > 0 and v.connu and label and label not in membres:
            db.ensure_membre(label)                       # premier passage d'un membre : auto-enregistrement
            membres = _membres(force=True)
        etat["vision"] = v
        # Consignation (limitée) de la détection
        d = _derniere_detection
        if v.visages > 0 and (label != d["label"] or v.connu != d["connu"] or now - d["ts"] > VISION_PERSIST_S):
            fiche = membres.get(label) if label else None
            etat["id_detection"] = db.insert_detection(label, v.confiance, v.connu,
                                                       fiche["id"] if fiche and v.connu else None)
            d.update(label=label, connu=v.connu, ts=now)
        evaluer_et_agir()          # réaction immédiate : pas besoin d'attendre le prochain message capteurs


def traiter_camera_etat(data):
    with _lock:
        etat["camera"] = {**data, "recu_le": time.time()}


def on_connect(client, userdata, flags, rc):
    print(f"📡 Connecté au broker MQTT avec le code {rc}")
    client.subscribe(MQTT_TOPIC)
    client.subscribe(MQTT_TOPIC_VISION)
    client.subscribe(MQTT_TOPIC_CAMERA_ETAT)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        if msg.topic != MQTT_TOPIC_CAMERA_ETAT:      # l'état caméra (1 msg/s) est trop bavard pour la console
            print(f"📥 [{msg.topic}] {data}")
        if msg.topic == MQTT_TOPIC_CAMERA_ETAT:
            traiter_camera_etat(data)
        elif msg.topic == MQTT_TOPIC_VISION:
            traiter_vision(data)
        else:
            traiter_capteurs(data)
    except json.JSONDecodeError:
        print("❌ Erreur: Le message n'est pas au format JSON valide.")
    except Exception as e:
        print(f"❌ Erreur lors du traitement: {e}")

def start_mqtt():
    """Démarre le client MQTT dans un thread séparé"""
    global mqtt_client
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    mqtt_client = client
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_forever()
    except Exception as e:
        print(f"❌ Impossible de se connecter à MQTT: {e}")

# --- API Endpoints ---
@app.on_event("startup")
def startup_event():
    # Initialise la DB au démarrage
    db.init_db()
    # Lance MQTT en arrière-plan
    mqtt_thread = threading.Thread(target=start_mqtt, daemon=True)
    mqtt_thread.start()

@app.get("/")
def read_root():
    return {"status": "SENTINEL-X Backend Operational"}

@app.get("/api/data/history")
def get_history(limit: int = 50):
    """Renvoie les données historiques pour le Dashboard"""
    data = db.get_latest_data(limit=limit)
    return {"history": data}

CAMERA_HORS_LIGNE_S = 5.0      # pas d'état reçu depuis 5 s => vision.py n'est pas lancé


def _camera_publique():
    cam = etat["camera"]
    if cam is None or time.time() - cam["recu_le"] > CAMERA_HORS_LIGNE_S:
        return {"en_ligne": False, "actif": False, "mode": None, "flux": None}
    return {"en_ligne": True, "actif": bool(cam.get("actif")), "mode": cam.get("mode"), "erreur": cam.get("erreur"),
            "flux": cam.get("flux")}


class CommandeCamera(BaseModel):
    action: Literal["on", "auto"]


@app.post("/api/camera")
def commande_camera(cmd: CommandeCamera):
    """Bouton du front : `on` force la caméra allumée, `auto` la rend pilotée par le détecteur de présence."""
    if mqtt_client is None:
        raise HTTPException(status_code=503, detail="MQTT indisponible")
    mqtt_client.publish(MQTT_TOPIC_CAMERA_CMD, json.dumps({"action": cmd.action}))
    return {"envoye": cmd.action}


@app.get("/api/status")
def get_status():
    """État en direct : capteurs, vision, analyse IA et commande envoyée à l'ESP (LED / buzzer / OLED)."""
    # Sans verrou : le thread MQTT garde _lock pendant ses accès à la base, et le front ne doit pas attendre.
    # Chaque valeur de `etat` est remplacée d'un bloc (jamais modifiée en place) : la lire est sans risque.
    capteurs, vision, env, decision = etat["capteurs"], etat["vision"], etat["env"], etat["decision"]
    return {
        "capteurs": asdict(capteurs) if capteurs else None,
        "vision": asdict(vision) if vision else None,
        "env": asdict(env) if env else None,
        "decision": decision.to_dict() if decision else None,
        "camera": _camera_publique(),
    }

@app.get("/api/analytics")
def get_analytics():
    """Analyse sur 5 min (moyennes par minute, variations %/min, seuils) pour température, humidité, gaz."""
    analytique.analyser()
    return analytique.dernier or {"niveau": 0, "metriques": {}}

@app.get("/api/events")
def get_events(limit: int = 50):
    """Journal des événements (accès autorisés, intrusions, alertes environnementales)."""
    return {"events": db.get_evenements(limit=limit)}

@app.post("/api/events/{id_evenement}/ack")
def ack_event(id_evenement: int):
    """Acquittement d'un événement par le superviseur."""
    if not db.acquitter_evenement(id_evenement):
        raise HTTPException(status_code=404, detail="Événement introuvable")
    return {"acquitte": True}

if __name__ == "__main__":
    print("🚀 Démarrage du serveur API SENTINEL-X")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
