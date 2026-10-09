#!/usr/bin/env python3
"""
SENTINEL-X : lanceur unique. Remplace les 5 terminaux par un seul :

    python lancer.py                  (ou double-clic sur lancer.bat sous Windows)

Au PREMIER lancement sur un PC, il vous guide : bibliothèques manquantes, Docker, choix des caméras, mot de passe.
Ensuite il démarre tout (base + broker, API et tableau de bord, caméra du poste, caméra de la porte) et ouvre
http://127.0.0.1:8000/ . Ctrl+C arrête l'ensemble.

Options utiles :
    --simulateur [scenario]   joue un scénario sans matériel (membre, inconnu, gaz, incendie, demo...)
    --esp IP                  relaie les mesures de l'ESP (IP affichée sur son écran LCD)
    --dev                     entrer sans authentification (développement seulement, depuis ce PC)
    --sans-porte              ne lance pas la caméra du boîtier (tester seulement la connexion)
    --sans-poste              ne lance pas la caméra d'authentification
    --reconfigurer            refait le choix des caméras
    --mot-de-passe            définit / change un mot de passe
    --sans-docker             Docker déjà lancé à la main
    --verbeux                 affiche aussi les avertissements techniques (OpenCV, scikit-learn...)
"""
import argparse
import contextlib
import getpass
import importlib
import importlib.util
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

RACINE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(RACINE, "backend")
INFRA = os.path.join(RACINE, "infra")
URL = "http://127.0.0.1:8000/"
EQUIPE = [n.strip() for n in os.getenv("SENTINEL_EQUIPE", "Caroline,Florent,Killian").split(",") if n.strip()]
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_FICHIER = os.path.join(RACINE, "ai", "face_detection_yunet_2023mar.onnx")
MODELE_VISAGE = os.path.join(RACINE, "ai", "V2.pkcls")

PAQUETS = [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("paho.mqtt.client", "paho-mqtt==1.6.1"),
           ("psycopg2", "psycopg2-binary"), ("cv2", "opencv-python"), ("numpy", "numpy>=2"),
           ("Orange", "orange3==3.40.0"), ("orangecontrib.imageanalytics", "Orange3-ImageAnalytics"),
           ("PyQt5", "PyQt5")]

# Lignes techniques sans intérêt pour l'utilisateur (masquées sauf --verbeux)
BRUIT = ("InconsistentVersionWarning", "warnings.warn(", "model_persistence.html", "[ WARN:", "[ERROR:0@",
         "on_event is deprecated", "Read more about it", "@app.on_event", "lifespan")

_verrou_affichage = threading.Lock()
VERBEUX = False


# ---------------------------------------------------------------- affichage
def afficher(nom, couleur, texte):
    with _verrou_affichage:
        print(f"\033[{couleur}m[{nom:<6}]\033[0m {texte}", flush=True)


def titre(texte):
    print(f"\n\033[1m=== {texte} ===\033[0m", flush=True)


def oui_non(question, defaut=True):
    if not sys.stdin.isatty():
        return defaut
    reponse = input(f"{question} {'[O/n]' if defaut else '[o/N]'} ").strip().lower()
    return defaut if not reponse else reponse in ("o", "oui", "y", "yes")


def quitter(message, code=1):
    print(f"\n❌ {message}", flush=True)
    sys.exit(code)


# ---------------------------------------------------------------- étape 1 : bibliothèques Python
def paquets_manquants():
    manquants = []
    for module, paquet in PAQUETS:
        try:
            absent = importlib.util.find_spec(module) is None
        except (ImportError, ValueError):
            absent = True
        if absent:
            manquants.append(paquet)
    return manquants


def verifier_dependances():
    titre("Bibliothèques Python")
    manquants = paquets_manquants()
    if manquants:
        print("Il manque : " + ", ".join(manquants))
        if oui_non("Les installer maintenant (pip install -r backend/requirements.txt) ?"):
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", os.path.join(BACKEND, "requirements.txt")])
            manquants = paquets_manquants()
        if manquants:
            quitter("Bibliothèques manquantes : " + ", ".join(manquants) +
                    f"\n   Installer avec :  {os.path.basename(sys.executable)} -m pip install -r backend/requirements.txt")
    try:
        from importlib.metadata import version
        if int(version("numpy").split(".")[0]) < 2:
            quitter("NumPy 2 ou plus est nécessaire (le modèle d'IA a été enregistré avec NumPy 2) :\n"
                    "   python -m pip install --upgrade \"numpy>=2\"")
    except Exception:
        pass
    print("✅ Bibliothèques présentes")


# ---------------------------------------------------------------- étape 2 : fichiers d'IA
def verifier_modeles():
    titre("Modèles d'IA")
    if not os.path.isfile(MODELE_VISAGE):
        quitter(f"Modèle de reconnaissance introuvable : {MODELE_VISAGE}\n"
                "   (il doit être dans le dépôt Git : git pull)")
    if not os.path.isfile(YUNET_FICHIER):
        print("Détecteur de visages absent, téléchargement (230 Ko)…")
        try:
            urllib.request.urlretrieve(YUNET_URL, YUNET_FICHIER)
        except Exception as e:
            quitter(f"Téléchargement impossible ({e}).\n   Télécharger {YUNET_URL}\n   puis le placer dans ai/")
    print("✅ Modèles présents")


# ---------------------------------------------------------------- étape 3 : Docker (base + broker MQTT)
def commande_compose():
    for cmd in (["docker", "compose"], ["docker-compose"]):
        try:
            if subprocess.run(cmd + ["version"], capture_output=True, timeout=30).returncode == 0:
                return cmd
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def demarrer_docker():
    titre("Docker : base de données et broker MQTT")
    compose = commande_compose()
    if compose is None:
        quitter("Docker est introuvable. Installer Docker Desktop, le lancer, attendre qu'il soit prêt, puis relancer.")
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode != 0:
            raise OSError
    except (OSError, subprocess.TimeoutExpired):
        quitter("Docker est installé mais ne répond pas : lancer Docker Desktop, attendre le voyant vert, puis relancer.")
    if subprocess.run(compose + ["up", "-d"], cwd=INFRA).returncode != 0:
        quitter("« docker compose up -d » a échoué (voir le message ci-dessus ; port 1883 ou 5433 déjà pris ?).")
    sys.path.insert(0, BACKEND)
    import db
    print("Attente de la base de données…", end="", flush=True)
    for _ in range(60):
        with contextlib.redirect_stdout(io.StringIO()):
            conn = db.get_db_connection()
        if conn is not None:
            conn.close()
            print(" prête.")
            return
        print(".", end="", flush=True)
        time.sleep(1)
    quitter("La base de données ne répond pas (docker logs sentinel-db).")


# ---------------------------------------------------------------- étape 4 : caméras
def configurer_cameras(forcer):
    titre("Caméras")
    sys.path.insert(0, BACKEND)
    import cameras
    cfg = cameras._lire()
    if not forcer and "porte" in cfg and "poste" in cfg:
        print(f"✅ Déjà réglées (porte : index {cfg['porte']}, poste : index {cfg['poste']}). "
              "Pour changer : python lancer.py --reconfigurer")
        return
    trouvees = cameras.lister()
    if not trouvees:
        print("⚠️  Aucune caméra détectée : la connexion par visage sera impossible (simulateur seulement).")
        return
    if len(trouvees) == 1:
        print(f"Une seule caméra (index {trouvees[0]}) : utilisée pour la porte et pour l'authentification.")
        cameras.enregistrer("porte", trouvees[0])
        cameras.enregistrer("poste", trouvees[0])
        return
    print("\nCaméra de la PORTE (celle du boîtier = la caméra USB externe) :")
    print("une fenêtre montre chaque caméra ; Entrée = « c'est celle-ci », Espace = suivante.")
    porte = cameras.choisir("porte", trouvees=trouvees)
    if porte is None:
        print("Aucune caméra choisie : réglage ignoré (relancer avec --reconfigurer).")
        return
    cameras.enregistrer("porte", porte)
    if oui_non("Utiliser la MÊME caméra pour l'authentification au tableau de bord ? "
               "(recommandé : c'est celle sur laquelle l'IA reconnaît le mieux)"):
        cameras.enregistrer("poste", porte)
        print("(La porte libère automatiquement la caméra pendant une connexion.)")
    else:
        print("\nCaméra du POSTE (authentification) :")
        poste = cameras.choisir("poste", trouvees=trouvees)
        if poste is None:
            cameras.enregistrer("poste", porte)
        else:
            cameras.enregistrer("poste", poste)


# ---------------------------------------------------------------- étape 5 : mots de passe
def saisir_mot_de_passe(nom):
    import auth
    for _ in range(3):
        mdp = getpass.getpass(f"  Mot de passe pour {nom} (8 caractères min., majuscule, minuscule, chiffre) : ")
        probleme = auth.verifier_force(mdp)
        if probleme:
            print(f"  ❌ Refusé : {probleme}.")
            continue
        if getpass.getpass("  Confirmer : ") != mdp:
            print("  ❌ Les deux saisies sont différentes.")
            continue
        return mdp
    return None


def configurer_mots_de_passe(forcer):
    titre("Mot de passe du tableau de bord")
    sys.path.insert(0, BACKEND)
    import auth
    import db
    with contextlib.redirect_stdout(io.StringIO()):
        db.init_db()
    avec_mdp = {label for label, _actif, a_mdp in db.list_membres_auth() if a_mdp}
    if avec_mdp and not forcer:
        print(f"✅ Mot de passe défini pour : {', '.join(sorted(avec_mdp))}. "
              "Pour en ajouter/changer : python lancer.py --mot-de-passe")
        return
    if not sys.stdin.isatty():
        print("⚠️  Terminal non interactif : lancer « python backend/manage_users.py mdp <Prénom> ».")
        return
    print("La connexion se fait par reconnaissance du visage PUIS mot de passe.")
    print("Les mots de passe restent sur ce PC (base locale), ils ne sont jamais envoyés sur Git.")
    while True:
        print("\nQui es-tu ?")
        for i, nom in enumerate(EQUIPE, 1):
            print(f"  {i}. {nom}" + ("  (mot de passe déjà défini)" if nom in avec_mdp else ""))
        choix = input("Numéro (Entrée pour terminer) : ").strip()
        if not choix:
            break
        if not (choix.isdigit() and 1 <= int(choix) <= len(EQUIPE)):
            print("Choix invalide.")
            continue
        nom = EQUIPE[int(choix) - 1]
        mdp = saisir_mot_de_passe(nom)
        if mdp is None:
            print("Abandon pour ce membre.")
            continue
        if db.set_mot_de_passe_hash(nom, auth.hash_password(mdp)):
            avec_mdp.add(nom)
            print(f"✅ Mot de passe de {nom} enregistré.")
        if not oui_non("Définir le mot de passe d'un autre membre ?", defaut=False):
            break
    if not avec_mdp:
        print("⚠️  Aucun mot de passe défini : impossible de se connecter au tableau de bord.")


# ---------------------------------------------------------------- étape 6 : services
class Service:
    def __init__(self, nom, couleur, args, env_sup=None, critique=False):
        self.nom, self.couleur, self.args = nom, couleur, args
        self.env_sup, self.critique = env_sup or {}, critique
        self.p = None
        self.signale = False

    def demarrer(self):
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", API_RELOAD="0", OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS="0")
        env.update(self.env_sup)
        options = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP    # Ctrl+C ne les atteint pas : on les arrête nous-mêmes
        else:
            options["start_new_session"] = True
        self.p = subprocess.Popen([sys.executable] + self.args, cwd=BACKEND, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                  bufsize=1, **options)
        threading.Thread(target=self._lire, daemon=True).start()

    def _lire(self):
        for ligne in self.p.stdout:
            ligne = ligne.rstrip()
            if not ligne.strip():
                continue
            if not VERBEUX and any(b in ligne for b in BRUIT):
                continue
            afficher(self.nom, self.couleur, ligne)

    def vivant(self):
        return self.p is not None and self.p.poll() is None

    def arreter(self):
        if not self.vivant():
            return
        if os.name == "nt":
            if self.nom == "ESP":                      # laisse le pont se déconnecter proprement de l'ESP
                with contextlib.suppress(Exception):
                    self.p.send_signal(signal.CTRL_BREAK_EVENT)
                    self.p.wait(timeout=3)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.p.pid, signal.SIGINT)
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.p.pid, signal.SIGKILL)


def port_utilise(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def api_prete():
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def lancer_services(args):
    titre("Démarrage")
    if port_utilise(8000):
        quitter("Le port 8000 est déjà utilisé (un ancien « python main.py » tourne encore ?). "
                "Le fermer, puis relancer.")
    if args.dev:
        args.sans_poste = True
        print("\033[33m⚠️  MODE DÉVELOPPEUR : connexion sans visage ni mot de passe. Jamais pour une démo ou une livraison.\033[0m")
    services = [Service("API", "36", ["main.py"], env_sup={**({"SENTINEL_DEV": "1"} if args.dev else {}), **({"SENTINEL_ENV": "0"} if args.sans_env else {}),
                                                    **({"SENTINEL_METEO": "0"} if args.sans_meteo else {})}, critique=True)]
    if not args.sans_poste:
        services.append(Service("POSTE", "35", ["vision_poste.py"]))
    if not args.sans_porte:
        services.append(Service("PORTE", "33", ["vision.py"]))
    if args.simulateur:
        services.append(Service("SIMU", "32", ["simulator.py", args.simulateur]))
    if args.esp:
        services.append(Service("ESP", "36", ["pont_esp.py", "--esp", args.esp] + (["--fahrenheit"] if args.esp_fahrenheit else []) + (["--capteurs", args.esp_capteurs] if args.esp_capteurs else []) + (["--seuil", str(args.esp_seuil)] if args.esp_seuil else [])))

    api = services[0]
    api.demarrer()
    print("Démarrage de l'API…", flush=True)
    for _ in range(90):
        if not api.vivant():
            quitter("L'API s'est arrêtée au démarrage (voir les messages ci-dessus).")
        if api_prete():
            break
        time.sleep(1)
    else:
        for s in services:
            s.arreter()
        quitter("L'API ne répond pas après 90 s.")
    for s in services[1:]:
        s.demarrer()

    print(f"\n✅ SENTINEL-X est prêt : {URL}")
    print("   Les caméras mettent 20 à 40 s à charger l'IA ; la connexion fonctionne dès que « POSTE » affiche "
          "« Caméra du poste en veille ».")
    print("   Ctrl+C pour tout arrêter.\n", flush=True)
    if not args.sans_navigateur:
        webbrowser.open(URL)

    try:
        while True:
            time.sleep(1)
            if not api.vivant():
                print("\n❌ L'API s'est arrêtée : arrêt de l'ensemble.")
                break
            for s in services[1:]:
                if not s.vivant() and not s.signale:
                    s.signale = True
                    afficher(s.nom, s.couleur, f"⚠️ arrêté (code {s.p.returncode}). Les autres services continuent.")
    except KeyboardInterrupt:
        print("\n🛑 Arrêt en cours…", flush=True)
    finally:
        for s in reversed(services):
            s.arreter()
        print("Arrêté. (La base et le broker restent actifs dans Docker : « docker compose down » dans infra pour les couper.)")


# ---------------------------------------------------------------- programme principal
def main():
    global VERBEUX
    ap = argparse.ArgumentParser(description="Lanceur SENTINEL-X", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--simulateur", nargs="?", const="demo", metavar="SCENARIO",
                    help="joue un scénario sans matériel (défaut : demo)")
    ap.add_argument("--esp-fahrenheit", action="store_true", help="la température de l'ESP est en °F (convertie en °C)")
    ap.add_argument("--esp-seuil", type=float, metavar="CM",
                    help="distance de détection de présence en cm (défaut 60)")
    ap.add_argument("--esp-capteurs", metavar="LISTE",
                    help="capteurs de l'ESP à interroger, ex. temp,dist,hum,gaz (défaut : temp,dist)")
    ap.add_argument("--sans-env", action="store_true",
                    help="n'envoie ni message ni buzzer pour les alertes d'environnement (gaz, température, humidité)")
    ap.add_argument("--sans-meteo", action="store_true",
                    help="désactive les prévisions météo (onglet Prévisions) : aucun appel à Internet")
    ap.add_argument("--sans-porte", action="store_true")
    ap.add_argument("--dev", action="store_true",
                    help="mode développeur : bouton d'entrée sans visage ni mot de passe (pas de caméra du poste)")
    ap.add_argument("--esp", metavar="IP", help="adresse IP de l'ESP (affichée sur son écran) : relaie ses mesures")
    ap.add_argument("--sans-poste", action="store_true")
    ap.add_argument("--reconfigurer", action="store_true")
    ap.add_argument("--mot-de-passe", action="store_true")
    ap.add_argument("--sans-docker", action="store_true")
    ap.add_argument("--sans-navigateur", action="store_true")
    ap.add_argument("--verbeux", action="store_true")
    args = ap.parse_args()
    VERBEUX = args.verbeux

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if os.name == "nt":
        os.system("")                       # active les couleurs ANSI de la console Windows
    print("\033[1mSENTINEL-X : lancement\033[0m")

    verifier_dependances()
    verifier_modeles()
    if not args.sans_docker:
        demarrer_docker()
    configurer_cameras(args.reconfigurer)
    configurer_mots_de_passe(args.mot_de_passe)
    lancer_services(args)


if __name__ == "__main__":
    main()
