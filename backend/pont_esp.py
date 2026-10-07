"""
Pont ESP -> SENTINEL-X.

Le firmware du boîtier (version de l'équipe) fait tourner un MINI-BROKER MQTT sur l'ESP lui-même (uMQTTBroker).
Il ne publie rien tout seul : il répond quand on l'interroge.

    demande   esp/request/temp   ->  réponse  esp/response/temp   {"result":"23.5"}
    demande   esp/request/dist   ->  réponse  esp/response/dist   {"result":"87"}

Ce script interroge l'ESP toutes les PERIODE_S secondes et republie le résultat, au format attendu par le back,
sur le broker Mosquitto local (topic `sentinel/sensors`) :

    {"temperature": 23.5, "presence": 1, "distance_cm": 87.0}

Présence = distance mesurée inférieure à SEUIL_PRESENCE_CM (ici le capteur de distance joue le rôle de détecteur).
Les clés absentes (humidité, gaz) sont simplement omises : le back les accepte facultatives.

    python pont_esp.py --esp 10.235.154.64          (l'adresse s'affiche sur l'écran LCD de l'ESP)
    ESP_HOST=10.235.154.64 python pont_esp.py
"""
import argparse
import contextlib
import json
import os
import re
import signal
import threading
import time

import paho.mqtt.client as mqtt

LOCAL_BROKER = os.getenv("MQTT_BROKER", "localhost")
LOCAL_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_SORTIE = "sentinel/sensors"
PERIODE_S = float(os.getenv("ESP_PERIODE_S", "2"))
SEUIL_PRESENCE_CM = float(os.getenv("ESP_SEUIL_PRESENCE_CM", "60"))
MAINTIEN_PRESENCE_S = float(os.getenv("ESP_MAINTIEN_PRESENCE_S", "3"))   # évite les clignotements de présence
TOPIC_COMMANDES = "sentinel/commandes"      # back -> pont : {"buzzer": 0|1|2, "ligne1": "...", "ligne2": "..."}
TOPIC_ESP_ECRAN = "esp/cmd/ecran"           # pont -> ESP   : "buzzer|ligne1|ligne2" (voir iot/PATCH_ESP_ECRAN_BUZZER.md)
NOMS = {"temp": "température", "dist": "distance", "hum": "humidité", "gaz": "gaz"}
VALEURS = {k: None for k in NOMS}
VERROU = threading.Lock()
RECUS = {k: threading.Event() for k in NOMS}


def lire_nombre(payload):
    """Extrait un nombre de {"result":"23.5"} (ou d'un texte brut). None si illisible."""
    texte = payload.decode(errors="replace")
    try:
        brut = json.loads(texte)
        if isinstance(brut, dict):
            texte = str(brut.get("result", ""))
        else:
            texte = str(brut)
    except ValueError:
        pass
    m = re.search(r"-?\d+(?:[.,]\d+)?", texte)
    return float(m.group().replace(",", ".")) if m else None


def chercher_esp():
    """Balaie le réseau local (port 1883) et liste les appareils qui répondent, hors ce PC."""
    import concurrent.futures
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        moi = s.getsockname()[0]
    except OSError:
        raise SystemExit("Réseau introuvable : le PC est-il connecté au Wi-Fi ?")
    finally:
        s.close()
    base = ".".join(moi.split(".")[:3])
    print(f"[ESP] recherche sur {base}.0/24 (ce PC : {moi})…")

    def test(i):
        ip = f"{base}.{i}"
        if ip == moi:
            return None
        try:
            with socket.create_connection((ip, 1883), timeout=0.6):
                return ip
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        trouves = [ip for ip in ex.map(test, range(1, 255)) if ip]
    if trouves:
        print("Appareils avec le port MQTT (1883) ouvert : " + ", ".join(trouves))
        print("L'ESP est l'un d'eux (comparer avec l'adresse affichée sur son écran LCD).")
    else:
        print("Aucun appareil MQTT trouvé : ESP éteint, hors du réseau, ou Wi-Fi qui isole les appareils.")


def main():
    ap = argparse.ArgumentParser(description="Pont ESP -> SENTINEL-X")
    ap.add_argument("--esp", default=os.getenv("ESP_HOST"), help="adresse IP de l'ESP (affichée sur son écran)")
    ap.add_argument("--port", type=int, default=int(os.getenv("ESP_PORT", "1883")))
    ap.add_argument("--fahrenheit", action="store_true", default=os.getenv("ESP_TEMP_F") == "1",
                    help="la température de l'ESP est en °F : la convertit en °C")
    ap.add_argument("--capteurs", default=os.getenv("ESP_CAPTEURS", "temp,dist"),
                    help="capteurs à interroger parmi temp,dist,hum,gaz (hum et gaz : seulement si le firmware les gère)")
    ap.add_argument("--seuil", type=float, default=SEUIL_PRESENCE_CM,
                    help="distance (cm) en dessous de laquelle quelqu'un est considéré présent (défaut %(default)g)")
    ap.add_argument("--chercher", action="store_true", help="cherche l'ESP sur le réseau (IP changée ?) puis quitte")
    args = ap.parse_args()
    if args.chercher:
        return chercher_esp()
    actifs = [c.strip() for c in args.capteurs.split(",") if c.strip() in NOMS]
    if not actifs:
        raise SystemExit("--capteurs : valeurs possibles temp, dist, hum, gaz")
    if not args.esp:
        raise SystemExit("Adresse de l'ESP manquante : python pont_esp.py --esp 10.235.154.64")

    local = mqtt.Client()
    local.reconnect_delay_set(1, 10)
    # Identifiant FIXE : l'ESP n'a que quelques places de connexion ; avec un identifiant fixe, une ancienne
    # connexion « fantôme » (arrêt brutal) est remplacée au lieu de s'accumuler jusqu'à saturer le mini-broker.
    esp = mqtt.Client(client_id="sentinelx-pont")
    esp.reconnect_delay_set(1, 10)

    def local_connecte(client, userdata, flags, rc):
        client.subscribe(TOPIC_COMMANDES)

    def local_message(client, userdata, msg):
        """Ordre du back (écran + buzzer) -> ESP."""
        try:
            c = json.loads(msg.payload.decode())
            texte = "{}|{}|{}".format(int(c.get("buzzer", 0)), str(c.get("ligne1", ""))[:16], str(c.get("ligne2", ""))[:16])
        except (ValueError, TypeError, AttributeError):
            return
        esp.publish(TOPIC_ESP_ECRAN, texte)

    local.on_connect = local_connecte
    local.on_message = local_message
    local.connect_async(LOCAL_BROKER, LOCAL_PORT, 60)
    local.loop_start()

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            print(f"[ESP] connexion perdue (code {rc}), nouvelle tentative…")

    def on_connect(client, userdata, flags, rc):
        print(f"[ESP] connecté à {args.esp}:{args.port}" if rc == 0 else f"[ESP] connexion refusée (code {rc})")
        client.subscribe("esp/response/#")

    def on_message(client, userdata, msg):
        cle = msg.topic.rsplit("/", 1)[-1]
        if cle in VALEURS:
            v = lire_nombre(msg.payload)
            if v is not None:
                with VERROU:
                    VALEURS[cle] = v
                RECUS[cle].set()

    esp.on_connect = on_connect
    esp.on_disconnect = on_disconnect
    esp.on_message = on_message
    esp.connect_async(args.esp, args.port, 15)
    esp.loop_start()
    for nom_sig in ("SIGTERM", "SIGBREAK"):          # arrêt « doux » demandé par lancer.py
        if hasattr(signal, nom_sig):
            signal.signal(getattr(signal, nom_sig), lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    debut_connexion = time.time()
    averti = False
    print(f"[ESP] interrogation de {args.esp} toutes les {PERIODE_S:g} s → {LOCAL_BROKER}:{LOCAL_PORT} {TOPIC_SORTIE}")

    sans_reponse = 0
    derniere_presence = 0.0
    bilan = {c: [0, 0] for c in actifs}           # [demandes, réponses reçues]
    dernier_bilan = time.time()
    try:
        while True:
            debut = time.time()
            if not esp.is_connected():
                if not averti and time.time() - debut_connexion > 8:
                    averti = True
                    print("[ESP] port 1883 joignable mais l'ESP ne répond pas au protocole MQTT : il est plein ou bloqué. "
                          "Le redémarrer (bouton reset / débrancher) puis relancer.")
                time.sleep(1)
                continue
            averti = False
            for cle in actifs:
                RECUS[cle].clear()
                esp.publish(f"esp/request/{cle}", "1")
                bilan[cle][0] += 1
                if RECUS[cle].wait(2.5):          # l'ESP attend jusqu'à 2 s la réponse de la carte capteurs
                    bilan[cle][1] += 1
            with VERROU:
                val = dict(VALEURS)
            if any(RECUS[c].is_set() for c in actifs):
                sans_reponse = 0
                msg = {}
                if RECUS["temp"].is_set() and "temp" in actifs:
                    t = val["temp"]
                    msg["temperature"] = round((t - 32) / 1.8, 1) if args.fahrenheit else t
                if RECUS["hum"].is_set() and "hum" in actifs:
                    msg["humidite"] = val["hum"]
                if RECUS["gaz"].is_set() and "gaz" in actifs:
                    msg["gaz"] = int(val["gaz"])
                if RECUS["dist"].is_set() and "dist" in actifs:
                    d = val["dist"]
                    msg["distance_cm"] = d
                    if d < args.seuil:
                        derniere_presence = time.time()
                    msg["presence"] = 1 if time.time() - derniere_presence < MAINTIEN_PRESENCE_S else 0
                local.publish(TOPIC_SORTIE, json.dumps(msg))
                manque = [NOMS[c] for c in actifs if not RECUS[c].is_set()]
                print(f"[ESP] {msg}" + (f"   ⚠️ pas de réponse pour : {', '.join(manque)}" if manque else ""))
            else:
                sans_reponse += 1
                if sans_reponse in (1, 5) or sans_reponse % 30 == 0:
                    print("[ESP] pas de réponse (ESP éteint, autre réseau, ou adresse incorrecte ?)")
            if time.time() - dernier_bilan >= 30:
                dernier_bilan = time.time()
                lignes = []
                for cle, (dem, rep) in bilan.items():
                    lignes.append(f"{cle} {rep}/{dem}" + ("" if dem == rep else f" ⚠️ {dem - rep} sans réponse"))
                print("[ESP] bilan (réponses/demandes) : " + " · ".join(lignes))
                for v in bilan.values():
                    v[0] = v[1] = 0
            time.sleep(max(0.0, PERIODE_S - (time.time() - debut)))
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(Exception):
            esp.disconnect()
        esp.loop_stop()
        local.loop_stop()


if __name__ == "__main__":
    main()
