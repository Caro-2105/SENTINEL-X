"""
Simulateur SENTINEL-X : rejoue les stories SANS matériel.

Il joue à la fois l'ESP8266 (publie `sentinel/sensors`), la caméra (publie `sentinel/vision`)
et affiche ce que l'ESP ferait en recevant `sentinel/commandes` (buzzer / OLED).

Usage :  python simulator.py <scénario> [--duree 40] [--membre Caroline]
Scénarios : normal | membre | confiance_faible | inconnu | sans_visage | usurpation | gaz | incendie | derive | demo | aleatoire
"""
import argparse
import json
import random
import time

import paho.mqtt.client as mqtt

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
TOPIC_CAPTEURS = "sentinel/sensors"
TOPIC_VISION = "sentinel/vision"
TOPIC_COMMANDES = "sentinel/commandes"
PERIODE_S = 2.0

BUZZER = {0: "silencieux", 1: "BIP intermittent", 2: "ALARME continue"}


def on_connect(client, userdata, flags, rc):
    print(f"✅ Simulateur connecté au broker (Code {rc})")
    client.subscribe(TOPIC_COMMANDES)


def on_message(client, userdata, msg):
    """ESP virtuel : affiche l'état des actionneurs."""
    try:
        c = json.loads(msg.payload.decode())
        print(f"      ESP ▸ 🔔 {BUZZER.get(c.get('buzzer'), '?'):<16} | OLED: [{c.get('ligne1')}] [{c.get('ligne2')}]")
    except Exception:
        pass


def bruit(x, amp):
    return round(x + random.uniform(-amp, amp), 1)


def etat_normal(t):
    return dict(temperature=bruit(22.0, 0.2), humidite=bruit(48.0, 0.5), gaz=int(random.uniform(140, 160)),
                presence=0, ir_temp=None, rfid_uid=None)


def vision(label=None, confiance=0.0, connu=False, visages=0):
    return dict(label=label, confiance=confiance, connu=connu, visages=visages)


def scenario(nom, membre):
    """Générateur de (capteurs, vision) : un tuple par tick de 2 s."""
    t = 0
    while True:
        s = etat_normal(t)
        v = vision()
        if nom == "normal":
            pass
        elif nom == "membre":
            s.update(presence=1, ir_temp=bruit(34.5, 0.5))
            v = vision(membre, round(random.uniform(0.88, 0.97), 2), True, 1)
        elif nom == "confiance_faible":    # membre reconnu par le modèle, mais sous 80 % de confiance
            s.update(presence=1, ir_temp=bruit(34.5, 0.5))
            v = vision(membre, round(random.uniform(0.60, 0.78), 2), True, 1)
        elif nom == "inconnu":
            s.update(presence=1, ir_temp=bruit(34.0, 0.5))
            v = vision("inconnu", round(random.uniform(0.3, 0.5), 2), False, 1)
        elif nom == "sans_visage":
            s.update(presence=1, ir_temp=bruit(33.0, 0.5))
        elif nom == "usurpation":          # photo d'un membre devant la caméra : visage OK, mais pas de chaleur
            s.update(presence=1, ir_temp=bruit(21.5, 0.3))
            v = vision(membre, 0.95, True, 1)
        elif nom == "gaz":                 # fuite de gaz progressive
            if t >= 15:
                s["gaz"] = 150 + (t - 15) * 14
        elif nom == "incendie":            # montée conjointe température + fumée
            if t >= 15:
                s["temperature"] = bruit(22.0 + (t - 15) * 0.25, 0.1)
                s["gaz"] = 150 + (t - 15) * 12
        elif nom == "derive":              # exemple du sujet : hausse lente de T° + micro-déviation du gaz
            if t >= 15:
                s["temperature"] = bruit(22.0 + (t - 15) * 0.05, 0.05)
                s["gaz"] = int(150 + (t - 15) * 0.9)
        elif nom == "aleatoire":           # ancien comportement du simulateur
            s = dict(temperature=round(random.uniform(20.0, 30.0), 1), humidite=round(random.uniform(40.0, 60.0), 1),
                     gaz=random.randint(100, 300), presence=random.choice([0, 0, 0, 1]),
                     ir_temp=None, rfid_uid=random.choice([None, None, "A1 B2 C3 D4", "99 88 77 66"]))
            if s["temperature"] > 28.0:
                s["gaz"] = random.randint(600, 900)
        yield s, v
        t += 1


def demo(membre):
    """Enchaîne toutes les stories (nombre de ticks de 2 s par scénario)."""
    plan = [("normal", 8), ("membre", 8), ("inconnu", 8), ("usurpation", 8), ("sans_visage", 8),
            ("normal", 20), ("gaz", 55), ("normal", 40), ("incendie", 40)]
    for nom, ticks in plan:
        print(f"\n===== Scénario : {nom} =====")
        gen = scenario(nom, membre)
        for _ in range(ticks):
            yield next(gen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", default="normal")
    ap.add_argument("--duree", type=int, default=0, help="durée en secondes (0 = infini)")
    ap.add_argument("--membre", default="Caroline", help="identité renvoyée par la 'caméra'")
    ap.add_argument("--broker", default=MQTT_BROKER)
    args = ap.parse_args()

    client = mqtt.Client("Simulateur_ESP8266")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(args.broker, MQTT_PORT, 60)
    except Exception as e:
        print(f"❌ Erreur de connexion MQTT: {e}")
        raise SystemExit(1)
    client.loop_start()

    flux = demo(args.membre) if args.scenario == "demo" else scenario(args.scenario, args.membre)
    print(f"🚀 Scénario '{args.scenario}' : un envoi toutes les {PERIODE_S:.0f} s. Ctrl+C pour arrêter.")
    debut = time.time()
    try:
        for capteurs, vis in flux:
            client.publish(TOPIC_VISION, json.dumps(vis))
            client.publish(TOPIC_CAPTEURS, json.dumps(capteurs))
            print(f"📡 capteurs={capteurs}  vision={vis}")
            time.sleep(PERIODE_S)
            if args.duree and time.time() - debut > args.duree:
                break
    except KeyboardInterrupt:
        pass
    print("\n🛑 Arrêt du simulateur.")
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
