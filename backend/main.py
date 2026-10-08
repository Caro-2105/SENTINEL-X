import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal
import paho.mqtt.client as mqtt
import json
import threading
import time
from dataclasses import asdict
import uvicorn
import auth
import db
from scenarios import MoteurScenarios, Capteurs, Vision
from analytics import AnalyticsAI

app = FastAPI(title="SENTINEL-X API")

# --- Authentification (visage puis mot de passe, voir auth.py) ---
# L'étape 1 s'appuie sur la webcam du PC (vision_poste.py), pas sur la caméra du boîtier de la porte.
# Pas de détecteur de présence ici : une photo présentée à la webcam peut valider l'étape 1, le mot de passe reste la barrière.
authent = auth.Authentificateur()


def utilisateur_courant(authorization: Optional[str] = Header(default=None)):
    """Dépendance FastAPI : exige un jeton de session valide (en-tête `Authorization: Bearer <jeton>`)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentification requise")
    session = authent.verifier(authorization[7:].strip(), time.time())
    if session is None:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée")
    return session


PROTEGE = [Depends(utilisateur_courant)]


def _est_admin(session):
    """Administrateur = compte marqué admin, ou session développeur.
    Amorçage : tant qu'aucun administrateur n'existe, toute session authentifiée peut en créer un."""
    if session["utilisateur"] == "dev":
        return True
    n = db.nb_admins_actifs()
    if n is None:
        raise HTTPException(status_code=503, detail="Base de données indisponible")
    return n == 0 or db.est_admin(session["utilisateur"])


def admin_requis(session: dict = Depends(utilisateur_courant)):
    if not _est_admin(session):
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs")
    return session


ADMIN = [Depends(admin_requis)]
# Aperçu de la caméra sur la mire de connexion (comme un déverrouillage facial). AUTH_APERCU_LOGIN=0 pour le retirer.
AUTH_APERCU_LOGIN = os.getenv("AUTH_APERCU_LOGIN", "1") != "0"

# Autoriser les requêtes CORS pour le Dashboard (Front-end local)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration MQTT ---
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "sentinel/sensors/#"          # ESP8266 -> serveur (capteurs)
MQTT_TOPIC_VISION = "sentinel/vision"      # script caméra -> serveur (identité)
MQTT_TOPIC_COMMANDES = "sentinel/commandes"  # serveur -> ESP8266 (buzzer, OLED)
MQTT_TOPIC_CAMERA_ETAT = "sentinel/camera/state"  # vision.py -> serveur (caméra allumée ? mode ?)
MQTT_TOPIC_CAMERA_CMD = "sentinel/camera/cmd"      # serveur -> vision.py (forcer / revenir en auto)
# Webcam du PC (authentification du tableau de bord), gérée par vision_poste.py : canaux distincts de la caméra de la porte
MQTT_TOPIC_POSTE_VISION = "sentinel/poste/vision"
MQTT_TOPIC_POSTE_ETAT = "sentinel/poste/state"
MQTT_TOPIC_POSTE_CMD = "sentinel/poste/cmd"
HEARTBEAT_COMMANDE_S = 2.0                 # la commande est ré-émise même sans changement
VISION_PERSIST_S = 10.0                    # une détection identique n'est consignée qu'une fois / 10 s

# --- État partagé (un seul thread MQTT écrit ; l'API lit) ---
moteur = MoteurScenarios()
analytique = AnalyticsAI()
_lock = threading.Lock()
mqtt_client = None
etat = {"poste": None, "camera": None, "capteurs": None, "vision": None, "env": None, "decision": None,
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


# Réaction (écran + buzzer + journal) aux alertes d'environnement ENV-01/02. ACTIVE par défaut ;
# SENTINEL_ENV=0 (ou « python lancer.py --sans-env ») pour ne tester que les scénarios d'accès.
ENV_ACTIF = os.getenv("SENTINEL_ENV", "1") != "0"
if not ENV_ACTIF:
    print("ℹ️  Alertes d'environnement désactivées (scénarios d'accès seulement).")


def evaluer_et_agir():
    """Rejoue le moteur de scénarios, commande les actionneurs de l'ESP et journalise."""
    now = time.time()
    membres = _membres()
    env = etat["env"] if ENV_ACTIF else None          # tests actuels : on ne réagit qu'aux scénarios d'ACCÈS
    decision = moteur.evaluer(etat["capteurs"], etat["vision"], env, membres, now)
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
            # seuls les scénarios fondés sur un visage (ACC-01/02/05/06) pointent vers une détection
            id_detection=etat["id_detection"] if ev.code in ("ACC-01", "ACC-02", "ACC-05", "ACC-06") else None,
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


def _commande_camera(action):
    print(f"🎛️  Commande caméra envoyée : {action}" + ("" if mqtt_client is not None else " (MQTT non connecté !)"))
    if mqtt_client is not None:
        mqtt_client.publish(MQTT_TOPIC_CAMERA_CMD, json.dumps({"action": action}))


def _commande_poste(action):
    if mqtt_client is not None:
        mqtt_client.publish(MQTT_TOPIC_POSTE_CMD, json.dumps({"action": action}))


def traiter_poste_etat(data):
    with _lock:
        etat["poste"] = {**data, "recu_le": time.time()}


def traiter_poste_vision(data):
    """Identification faite par la webcam du PC : sert UNIQUEMENT à l'étape 1 de la connexion (pas aux scénarios d'accès)."""
    label = data.get("label")
    now = time.time()
    confiance = float(data.get("confiance") or 0.0)
    with _lock:
        membres = _membres()
        if authent.sur_vision(label, confiance, bool(data.get("connu")), int(data.get("visages", 0)), membres, now):
            print(f"🔐 Étape 1 validée : visage de {label} reconnu ({confiance:.0%}) par la webcam du PC")
            _commande_poste("off")


def traiter_camera_etat(data):
    with _lock:
        etat["camera"] = {**data, "recu_le": time.time()}


def on_connect(client, userdata, flags, rc):
    print(f"📡 Connecté au broker MQTT avec le code {rc}")
    client.subscribe(MQTT_TOPIC)
    client.subscribe(MQTT_TOPIC_VISION)
    client.subscribe(MQTT_TOPIC_CAMERA_ETAT)
    client.subscribe(MQTT_TOPIC_POSTE_VISION)
    client.subscribe(MQTT_TOPIC_POSTE_ETAT)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        if msg.topic not in (MQTT_TOPIC_CAMERA_ETAT, MQTT_TOPIC_POSTE_ETAT):   # états (1 msg/s) : trop bavards
            print(f"📥 [{msg.topic}] {data}")
        if msg.topic == MQTT_TOPIC_CAMERA_ETAT:
            traiter_camera_etat(data)
        elif msg.topic == MQTT_TOPIC_POSTE_ETAT:
            traiter_poste_etat(data)
        elif msg.topic == MQTT_TOPIC_POSTE_VISION:
            traiter_poste_vision(data)
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

@app.get("/", include_in_schema=False)
def read_root():
    return RedirectResponse("/app/")          # le tableau de bord est servi par le backend : http://127.0.0.1:8000/


# MODE DÉVELOPPEUR : SENTINEL_DEV=1 (ou « python lancer.py --dev ») permet d'entrer sans visage ni mot de passe,
# uniquement depuis ce PC. À ne JAMAIS activer pour une démonstration ou une livraison.
DEV_MODE = os.getenv("SENTINEL_DEV", "0") == "1"
if DEV_MODE:
    print("⚠️  MODE DÉVELOPPEUR : l'authentification peut être contournée (depuis ce PC seulement).")


@app.get("/api/health")
def health():
    return {"status": "SENTINEL-X Backend Operational", "dev": DEV_MODE}


@app.post("/api/auth/dev")
def auth_dev(request: Request):
    """Session sans authentification : refusée (404) hors mode développeur ou hors de ce PC."""
    hote = request.client.host if request.client else ""
    if not DEV_MODE or hote not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=404, detail="Not Found")
    jeton, nom = authent.session_dev(time.time())
    print("⚠️  Connexion en mode développeur (sans authentification)")
    return {"token": jeton, "utilisateur": nom}

class DemandeConnexion(BaseModel):
    login_id: str
    mot_de_passe: str


@app.post("/api/auth/face/start")
def auth_face_start():
    """Étape 1 : ouvre une connexion et allume la caméra pour la reconnaissance du visage."""
    login_id = authent.demarrer(time.time())
    _commande_poste("on")                 # webcam du PC (et non la caméra de la porte)
    return {"login_id": login_id}


@app.get("/api/auth/face/{login_id}")
def auth_face_statut(login_id: str):
    st = authent.statut(login_id, time.time())
    cam = _poste_publique()
    if st["etape"] == "visage":
        # Diagnostic : on dit précisément ce qui manque plutôt qu'un vague "ça ne marche pas"
        if not cam["en_ligne"]:
            st["message"] = "Caméra du poste injoignable : lancer « python vision_poste.py »"
        elif cam.get("erreur"):
            st["message"] = f"{cam['erreur']}"
        elif not cam["actif"]:
            st["message"] = "Allumage de la caméra…"
        elif AUTH_APERCU_LOGIN and cam.get("flux"):
            st["apercu"] = cam["flux"]       # aperçu de son propre visage ; l'API n'écoute qu'en local par défaut
    return st


@app.post("/api/auth/login")
def auth_login(d: DemandeConnexion):
    """Étape 2 : mot de passe de l'utilisateur reconnu à l'étape 1."""
    try:
        jeton, nom = authent.connexion(d.login_id, d.mot_de_passe, db.get_mot_de_passe_hash, time.time())
    except auth.AuthError as e:
        print(f"🔐 Connexion refusée ({e.code}) : {e.message}")
        headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
        raise HTTPException(status_code=e.http, detail=e.detail(), headers=headers)
    print(f"🔐 Connexion réussie : {nom}")
    return {"token": jeton, "utilisateur": nom}


@app.get("/api/auth/me")
def auth_me(session: dict = Depends(utilisateur_courant)):
    try:
        admin = _est_admin(session)
    except HTTPException:
        admin = False
    return {"utilisateur": session["nom"], "admin": admin}


@app.post("/api/auth/logout")
def auth_logout(authorization: Optional[str] = Header(default=None)):
    if authorization and authorization.lower().startswith("bearer "):
        authent.deconnexion(authorization[7:].strip())
    return {"deconnecte": True}


# --- Administration des comptes (onglet « Administration » du front) ---
class NouveauCompte(BaseModel):
    label: str
    nom_affiche: Optional[str] = None
    mot_de_passe: str
    admin: bool = False


class ModifCompte(BaseModel):
    actif: Optional[bool] = None
    admin: Optional[bool] = None
    mot_de_passe: Optional[str] = None
    nom_affiche: Optional[str] = None


@app.get("/api/admin/membres")
def admin_liste(session: dict = Depends(admin_requis)):
    membres = db.list_membres_admin()
    if membres is None:
        raise HTTPException(status_code=503, detail="Base de données indisponible")
    return {"membres": membres, "moi": session["utilisateur"],
            "amorcage": not any(m["admin"] and m["actif"] for m in membres)}


@app.post("/api/admin/membres", status_code=201)
def admin_creer(d: NouveauCompte, session: dict = Depends(admin_requis)):
    label = d.label.strip()
    probleme = auth.verifier_label(label) or auth.verifier_force(d.mot_de_passe)
    if probleme:
        raise HTTPException(status_code=422, detail=f"Refusé : {probleme}.")
    nom = (d.nom_affiche or "").strip() or label
    cree = db.creer_membre(label, nom, auth.hash_password(d.mot_de_passe), d.admin)
    if cree is None:
        raise HTTPException(status_code=503, detail="Base de données indisponible")
    if not cree:
        raise HTTPException(status_code=409, detail=f"Le compte « {label} » existe déjà.")
    _membres(force=True)
    print(f"👤 Compte créé par {session['nom']} : {label}{' (admin)' if d.admin else ''}")
    return {"cree": label}


@app.patch("/api/admin/membres/{label}")
def admin_modifier(label: str, d: ModifCompte, session: dict = Depends(admin_requis)):
    cible = db.get_membre_admin(label)
    if cible is None:
        raise HTTPException(status_code=404, detail="Compte introuvable")
    hachage = None
    if d.mot_de_passe is not None:
        probleme = auth.verifier_force(d.mot_de_passe)
        if probleme:
            raise HTTPException(status_code=422, detail=f"Refusé : {probleme}.")
        hachage = auth.hash_password(d.mot_de_passe)
    perd_admin = (d.actif is False and cible["admin"]) or (d.admin is False and cible["admin"])
    if perd_admin and cible["actif"]:
        if label == session["utilisateur"]:
            raise HTTPException(status_code=400, detail="Vous ne pouvez pas retirer vos propres droits ni désactiver votre compte.")
        if (db.nb_admins_actifs() or 0) <= 1:
            raise HTTPException(status_code=400, detail="Il doit rester au moins un administrateur actif.")
    ok = db.maj_membre(label, actif=d.actif, admin=d.admin, mot_de_passe_hash=hachage,
                       nom_affiche=(d.nom_affiche or "").strip()[:40] or None)
    if ok is None:
        raise HTTPException(status_code=503, detail="Base de données indisponible")
    if not ok:
        raise HTTPException(status_code=422, detail="Aucune modification.")
    _membres(force=True)
    print(f"👤 Compte modifié par {session['nom']} : {label}")
    return {"modifie": label}


@app.get("/api/data/history", dependencies=PROTEGE)
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


def _poste_publique():
    p = etat["poste"]
    if p is None or time.time() - p["recu_le"] > CAMERA_HORS_LIGNE_S:
        return {"en_ligne": False, "actif": False, "erreur": None, "flux": None}
    return {"en_ligne": True, "actif": bool(p.get("actif")), "erreur": p.get("erreur"), "flux": p.get("flux")}


class CommandeCamera(BaseModel):
    action: Literal["on", "auto"]


@app.post("/api/camera", dependencies=PROTEGE)
def commande_camera(cmd: CommandeCamera):
    """Bouton du front : `on` force la caméra allumée, `auto` la rend pilotée par le détecteur de présence."""
    if mqtt_client is None:
        raise HTTPException(status_code=503, detail="MQTT indisponible")
    _commande_camera(cmd.action)
    return {"envoye": cmd.action}


@app.get("/api/status", dependencies=PROTEGE)
def get_status():
    """État en direct : capteurs, vision, analyse IA et commande envoyée à l'ESP (buzzer / OLED)."""
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

@app.get("/api/analytics", dependencies=PROTEGE)
def get_analytics():
    """Analyse sur 5 min (moyennes par minute, variations %/min, seuils) pour température, humidité, gaz."""
    analytique.analyser()
    return analytique.dernier or {"niveau": 0, "metriques": {}}

@app.get("/api/events", dependencies=PROTEGE)
def get_events(limit: int = 50):
    """Journal des événements (accès autorisés, intrusions, alertes environnementales)."""
    return {"events": db.get_evenements(limit=limit)}

@app.post("/api/events/{id_evenement}/ack", dependencies=PROTEGE)
def ack_event(id_evenement: int):
    """Acquittement d'un événement par le superviseur."""
    if not db.acquitter_evenement(id_evenement):
        raise HTTPException(status_code=404, detail="Événement introuvable")
    return {"acquitte": True}

# Tableau de bord (frontend/) servi par le backend : une seule adresse, pas de fichier à ouvrir à la main.
FRONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
if os.path.isdir(FRONT_DIR):
    app.mount("/app", StaticFiles(directory=FRONT_DIR, html=True), name="front")


if __name__ == "__main__":
    print("🚀 Démarrage du serveur API SENTINEL-X")
    # 127.0.0.1 : l'API (caméra, capteurs) n'est pas exposée au réseau. API_HOST=0.0.0.0 pour l'ouvrir sciemment.
    uvicorn.run("main:app", host=os.getenv("API_HOST", "127.0.0.1"), port=8000,
                reload=os.getenv("API_RELOAD", "0") == "1")   # API_RELOAD=1 : rechargement automatique pour le développement
