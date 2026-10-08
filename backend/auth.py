"""
Authentification SENTINEL-X : étape 1 = reconnaissance du visage, étape 2 = mot de passe.

Principe (appliqué par le BACKEND, la page de connexion n'est qu'une interface) :
  1. Le front demande une connexion (`demarrer`) : la caméra est allumée.
  2. Chaque identification publiée par vision_poste.py (`sur_vision`) est comparée aux membres actifs.
     Une identification d'un membre actif, avec exactement un visage et une confiance >= 85 %, valide
     l'étape 1 (AUTH_IDENTIFICATIONS=N pour en exiger N successives).
  3. L'utilisateur identifié saisit son mot de passe (`connexion`). Mots de passe hachés (scrypt + sel),
     5 essais puis verrouillage de plus en plus long.
  4. Un jeton de session aléatoire est délivré ; toutes les routes /api/* le demandent.

Aucun mot de passe n'est stocké en clair ni dans le code : `python manage_users.py mdp <Membre>`.
Limites connues : voir README (broker MQTT anonyme, photo présentée à la caméra si AUTH_EXIGE_PRESENCE=0).
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MDP_MIN = 8
MDP_MAX = 256

FACE_CONFIANCE_MIN = float(os.getenv("AUTH_CONFIANCE_MIN", "0.85"))
# Une seule identification au-dessus du seuil suffit (connexion rapide). Le mot de passe de l'étape 2 reste la vraie barrière.
# AUTH_IDENTIFICATIONS=3 exige 3 identifications successives du même membre (plus strict, ~3 s de plus).
FACE_CONSECUTIVES = max(1, int(os.getenv("AUTH_IDENTIFICATIONS", "1")))
LOGIN_TTL_S = 180.0             # temps laissé pour se faire reconnaître
FACE_VALIDE_S = 120.0          # temps laissé pour saisir le mot de passe après la reconnaissance
MAX_ESSAIS = 5                 # essais de mot de passe par reconnaissance
VERROU_S = 60.0                # 1er verrouillage ; double à chaque récidive
VERROU_MAX_S = 900.0
SESSION_INACTIVITE_S = 1800.0  # 30 min sans activité
SESSION_MAX_S = 8 * 3600.0
MAX_LOGINS_EN_ATTENTE = 20


# ---------------------------------------------------------------- mots de passe
def hash_password(mdp):
    sel = os.urandom(16)
    h = hashlib.scrypt(mdp.encode("utf-8"), salt=sel, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${sel.hex()}${h.hex()}"


def verify_password(mdp, stocke):
    try:
        algo, n, r, p, sel, h = stocke.split("$")
        if algo != "scrypt":
            return False
        calcule = hashlib.scrypt(mdp.encode("utf-8"), salt=bytes.fromhex(sel), n=int(n), r=int(r), p=int(p),
                                 dklen=len(h) // 2)
        return hmac.compare_digest(calcule.hex(), h)
    except Exception:
        return False


def verifier_force(mdp):
    """Message d'erreur si le mot de passe est trop faible, sinon None."""
    if len(mdp) < MDP_MIN:
        return f"au moins {MDP_MIN} caractères"
    if len(mdp) > MDP_MAX:
        return f"{MDP_MAX} caractères au maximum"
    if mdp.lower() == mdp or mdp.upper() == mdp or not any(c.isdigit() for c in mdp):
        return "mélanger majuscules, minuscules et au moins un chiffre"
    return None


LABEL_MAX = 40


def verifier_label(label):
    """Message d'erreur si l'identifiant de compte est invalide, sinon None.
    Doit être identique au nom de la classe du modèle de reconnaissance pour que le visage soit reconnu."""
    if not isinstance(label, str) or not label.strip():
        return "identifiant vide"
    if label != label.strip() or len(label) > LABEL_MAX:
        return f"{LABEL_MAX} caractères maximum, sans espace au début ni à la fin"
    if not all(c.isalnum() or c in " -_'" for c in label):
        return "lettres, chiffres, espace, tiret, tiret bas ou apostrophe uniquement"
    if label.lower() == "dev":
        return "« dev » est réservé au mode développeur"
    return None


_FAUX_HASH = hash_password("mot-de-passe-factice")   # même temps de calcul quand l'utilisateur n'a pas de mot de passe


class AuthError(Exception):
    def __init__(self, code, message, http=401, retry_after=None):
        super().__init__(message)
        self.code, self.message, self.http, self.retry_after = code, message, http, retry_after

    def detail(self):
        return {"code": self.code, "message": self.message, "retry_after": self.retry_after}


class Authentificateur:
    def __init__(self, exige_presence=False):
        self._lock = threading.Lock()
        self._logins = {}      # login_id -> état de la connexion en cours
        self._sessions = {}    # jeton -> session
        self._verrous = {}     # utilisateur -> fin du verrouillage
        self._recidives = {}   # utilisateur -> nombre de verrouillages
        self.exige_presence = exige_presence

    # ---------------------------------------------------------- étape 1 : visage
    def demarrer(self, now):
        with self._lock:
            self._purger(now)
            if len(self._logins) >= MAX_LOGINS_EN_ATTENTE:
                plus_ancien = min(self._logins, key=lambda k: self._logins[k]["cree"])
                del self._logins[plus_ancien]
            login_id = secrets.token_urlsafe(16)
            self._logins[login_id] = {"cree": now, "candidat": None, "n": 0, "utilisateur": None, "nom": None,
                                      "face_ts": None, "essais": 0, "message": "Placez-vous devant la caméra"}
            return login_id

    def _purger(self, now):
        for k in [k for k, v in self._logins.items() if self._expire(v, now)]:
            del self._logins[k]
        for t in [t for t, s in self._sessions.items()
                  if now - s["debut"] > SESSION_MAX_S or now - s["dernier"] > SESSION_INACTIVITE_S]:
            del self._sessions[t]

    @staticmethod
    def _expire(login, now):
        if login["utilisateur"] is None:
            return now - login["cree"] > LOGIN_TTL_S
        return now - login["face_ts"] > FACE_VALIDE_S

    def sur_vision(self, label, confiance, connu, visages, membres, now, presence_ok=True):
        """Compare une identification de la caméra aux connexions en attente. True si une étape 1 vient d'être validée."""
        vu = f" (modèle : {label} {int(confiance * 100)} %)" if label and label not in ("Vide", "inconnu") else ""
        if visages != 1:
            raison = ("Aucun visage détecté" if visages == 0 else "Plusieurs visages détectés") + vu
        elif not connu or not label:
            raison = "Visage non reconnu"
        elif label not in membres or not membres[label]["actif"]:
            raison = f"{label} n'a pas accès au tableau de bord"
        elif confiance < FACE_CONFIANCE_MIN:
            raison = f"Identification incertaine : {label} {int(confiance * 100)} % (minimum {int(FACE_CONFIANCE_MIN * 100)} %)"
        elif self.exige_presence and not presence_ok:
            raison = "Présence non confirmée par le détecteur"
        else:
            raison = None

        valide = False
        with self._lock:
            for login in self._logins.values():
                if login["utilisateur"] is not None or self._expire(login, now):
                    continue
                if raison is not None:
                    login["candidat"], login["n"], login["message"] = None, 0, raison
                    continue
                if login["candidat"] == label:
                    login["n"] += 1
                else:
                    login["candidat"], login["n"] = label, 1
                login["message"] = "Identification en cours…"
                if login["n"] >= FACE_CONSECUTIVES:
                    login["utilisateur"], login["nom"], login["face_ts"] = label, membres[label]["nom"], now
                    valide = True
        return valide

    def statut(self, login_id, now):
        with self._lock:
            login = self._logins.get(login_id)
            if login is None or self._expire(login, now):
                self._logins.pop(login_id, None)
                return {"etape": "expire", "message": "Session expirée, recommencez."}
            if login["utilisateur"] is not None:
                return {"etape": "mot_de_passe", "utilisateur": login["nom"], "message": "Visage reconnu"}
            return {"etape": "visage", "message": login["message"],
                    "progression": round(min(login["n"] / FACE_CONSECUTIVES, 1.0), 2)}

    # ---------------------------------------------------------- étape 2 : mot de passe
    def connexion(self, login_id, mdp, hash_de, now):
        """`hash_de(utilisateur)` renvoie le hachage enregistré (ou None). Renvoie (jeton, nom affiché)."""
        with self._lock:
            login = self._logins.get(login_id)
            if login is not None and self._expire(login, now):
                del self._logins[login_id]
                login = None
            if login is None or login["utilisateur"] is None:     # inconnu, expiré, ou visage pas encore validé
                raise AuthError("face_requise", "Reconnaissance du visage à refaire.", http=410)
            utilisateur, nom = login["utilisateur"], login["nom"]
            fin = self._verrous.get(utilisateur)
            if fin is not None and now < fin:
                raise AuthError("verrouille", f"Compte verrouillé, réessayez dans {int(fin - now) + 1} s.",
                                http=423, retry_after=int(fin - now) + 1)

        stocke = hash_de(utilisateur)
        if stocke is None:
            raise AuthError("sans_mot_de_passe", f"Aucun mot de passe défini pour {nom} : "
                            f"lancer « python manage_users.py mdp {utilisateur} ».", http=401)
        correct = isinstance(mdp, str) and len(mdp) <= MDP_MAX and verify_password(mdp, stocke or _FAUX_HASH) \
            and stocke is not None

        with self._lock:
            if not correct:
                login["essais"] += 1
                if login["essais"] >= MAX_ESSAIS:
                    n = self._recidives.get(utilisateur, 0) + 1
                    self._recidives[utilisateur] = n
                    duree = min(VERROU_S * 2 ** (n - 1), VERROU_MAX_S)
                    self._verrous[utilisateur] = now + duree
                    self._logins.pop(login_id, None)
                    raise AuthError("verrouille", f"Trop d'essais : compte verrouillé {int(duree)} s.",
                                    http=423, retry_after=int(duree))
                restant = MAX_ESSAIS - login["essais"]
                raise AuthError("mot_de_passe", f"Mot de passe incorrect ({restant} essai(s) restant(s)).")
            self._logins.pop(login_id, None)               # une reconnaissance ne sert qu'une fois
            self._recidives.pop(utilisateur, None)
            jeton = secrets.token_urlsafe(32)
            self._sessions[jeton] = {"utilisateur": utilisateur, "nom": nom, "debut": now, "dernier": now}
            return jeton, nom

    # ---------------------------------------------------------- sessions
    def verifier(self, jeton, now):
        with self._lock:
            s = self._sessions.get(jeton)
            if s is None:
                return None
            if now - s["debut"] > SESSION_MAX_S or now - s["dernier"] > SESSION_INACTIVITE_S:
                del self._sessions[jeton]
                return None
            s["dernier"] = now
            return dict(s)

    def session_dev(self, now):
        """Session SANS visage ni mot de passe (mode développeur, voir SENTINEL_DEV dans main.py)."""
        with self._lock:
            jeton = secrets.token_urlsafe(32)
            self._sessions[jeton] = {"utilisateur": "dev", "nom": "Dev", "debut": now, "dernier": now}
            return jeton, "Dev"

    def deconnexion(self, jeton):
        with self._lock:
            self._sessions.pop(jeton, None)
