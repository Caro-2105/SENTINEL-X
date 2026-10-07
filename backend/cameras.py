"""
Choix des caméras de SENTINEL-X (communs à vision.py = caméra de la PORTE et vision_poste.py = caméra du POSTE).

Les numéros de caméra dépendent de chaque PC (caméra intégrée = 0 ou 1 selon la machine) : on les règle donc par PC,
dans `camera_config.json` (non versionné), plutôt que dans le code.

Ordre de priorité : option --camera  >  variable d'environnement  >  camera_config.json  >  valeur par défaut.

    python vision.py --choisir            # montre chaque caméra dans une fenêtre : Entrée = « c'est celle-ci »
    python vision.py --camera 0 --enregistrer
    python vision.py --liste
"""
import json
import os

os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")   # évite 30 à 90 s d'ouverture de caméra sous Windows (MSMF)

import cv2

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_config.json")


def _lire():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def index(role, defaut, env=None):
    """Index de caméra pour un rôle ("porte" ou "poste")."""
    if env and os.getenv(env, "").strip().lstrip("-").isdigit():
        return int(os.environ[env])
    valeur = _lire().get(role)
    return int(valeur) if isinstance(valeur, int) else defaut


def enregistrer(role, idx):
    cfg = _lire()
    cfg[role] = int(idx)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"💾 Caméra « {role} » = index {idx}, enregistré dans camera_config.json (utilisé aux prochains lancements).")


def ouvrir_camera(idx):
    """Ouvre la caméra `idx` ; DirectShow seulement en secours sous Windows. None si impossible."""
    cap = cv2.VideoCapture(idx)
    if cap.isOpened():
        return cap
    cap.release()
    if os.name == "nt":
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap.isOpened():
            return cap
        cap.release()
    return None


def lister(maxi=4):
    """Affiche les index qui s'ouvrent réellement (quelques secondes par index absent). Renvoie la liste."""
    print("Recherche des caméras (index 0 à %d)…" % (maxi - 1))
    trouvees = []
    for i in range(maxi):
        print(f"  index {i} : ", end="", flush=True)
        cap = ouvrir_camera(i)
        if cap is None:
            print("aucune caméra")
            continue
        ok, frame = cap.read()
        taille = f"{frame.shape[1]}x{frame.shape[0]}" if ok else "image illisible"
        cap.release()
        print(f"caméra trouvée ({taille})")
        trouvees.append(i)
    if not trouvees:
        print("\nAucune caméra détectée (câble, accès caméra de Windows, ou caméra utilisée par une autre application).")
    return trouvees


def choisir(role, maxi=4, trouvees=None):
    """Montre chaque caméra dans une fenêtre pour reconnaître la bonne. Renvoie l'index choisi, ou None."""
    if trouvees is None:
        trouvees = lister(maxi)
    for i in trouvees:
        cap = ouvrir_camera(i)
        if cap is None:
            continue
        titre = f"Camera index {i} - Entree = c'est celle-ci, Espace = suivante, q = quitter"
        choix = None
        while choix is None:
            ok, frame = cap.read()
            if not ok:
                break
            cv2.imshow(titre, frame)
            touche = cv2.waitKey(30) & 0xFF
            if touche in (13, 10):          # Entrée
                choix = "oui"
            elif touche == 32:              # Espace
                choix = "suivante"
            elif touche == ord("q"):
                choix = "quitter"
        cap.release()
        cv2.destroyAllWindows()
        if choix == "oui":
            return i
        if choix == "quitter":
            break
    return None


def traiter_options(args, role, courant):
    """Applique --liste / --choisir / --camera / --enregistrer. Renvoie (index à utiliser, fini ?)."""
    if args.liste:
        lister()
        return courant, True
    idx = courant
    if args.choisir:
        choisi = choisir(role)
        if choisi is None:
            print("Aucune caméra choisie.")
            return courant, True
        idx = choisi
        args.enregistrer = True               # choisir sert à mémoriser le résultat
    elif args.camera is not None:
        idx = args.camera
    if args.enregistrer:
        enregistrer(role, idx)
    return idx, False


def ajouter_options(parser):
    parser.add_argument("--camera", type=int, help="index de la caméra à utiliser")
    parser.add_argument("--liste", action="store_true", help="liste les caméras détectées puis quitte")
    parser.add_argument("--choisir", action="store_true",
                        help="affiche chaque caméra pour choisir la bonne (puis l'enregistre) ")
    parser.add_argument("--enregistrer", action="store_true", help="mémorise l'index pour les prochains lancements")
