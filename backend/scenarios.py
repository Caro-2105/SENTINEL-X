"""
SENTINEL-X : moteur de scénarios ("stories").

Ce module ne fait AUCUNE entrée/sortie (ni MQTT, ni base de données, ni caméra) :
il reçoit l'état courant des capteurs, de la vision et de l'analyse IA environnementale,
et renvoie une Décision (buzzer, texte OLED) + les événements à journaliser.
Il est donc testable sans matériel (voir tests/test_scenarios.py) et rejouable avec simulator.py.

Catalogue des stories
---------------------
Accès (détecteur de présence + caméra + infrarouge) :
  ACC-01  Membre reconnu (confiance >= 80 %) + chaleur humaine -> buzzer 0 + "Bienvenue <label>"
  ACC-06  Membre reconnu mais confiance < 80 %  -> "CONFIANCE TROP FAIBLE" + la valeur du taux
  ACC-02  Visage inconnu                        -> buzzer + "INTRUSION"
  ACC-03  Présence, identification en cours     -> message d'attente (aucune alerte)
  ACC-04  Présence jamais identifiée (délai)    -> buzzer + "ALERTE PRESENCE"
  ACC-05  Visage connu SANS chaleur humaine     -> refus + buzzer (photo / écran présenté à la caméra)
Environnement (température, humidité, gaz/fumée -> analyse IA) :
  ENV-01  Anomalie / dérive détectée (niveau 1) -> buzzer intermittent + message
  ENV-02  Danger critique (niveau 2)            -> buzzer continu rapide + message
"""
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# --------------------------------------------------------------------------------------
# Paramètres métier (à ajuster pendant les tests avec le vrai matériel)
# --------------------------------------------------------------------------------------
VISION_TTL_S = 5.0            # un résultat caméra est valable 5 s
CAPTEURS_TTL_S = 10.0         # au-delà, on considère que l'ESP ne parle plus
CONFIANCE_MIN = 0.80          # en dessous, un visage "connu" n'est pas accepté (message "confiance trop faible")
IR_HUMAIN_MIN = 28.0          # plage de température de surface d'une peau (°C)
IR_HUMAIN_MAX = 42.0
DELAI_IDENTIFICATION_S = 8.0  # temps laissé à la caméra pour identifier une présence
MAINTIEN_ACCES_S = 8.0        # le message "Bienvenue" reste affiché après la dernière validation
FRAICHEUR_IR_S = 1.0          # la mesure IR ne doit pas être plus vieille que l'image du visage (désynchro caméra/ESP)
COOLDOWN_EVENEMENT_S = 30.0   # anti-doublon dans le journal
ECRAN_COLS = 21               # OLED 128x64, police 6x8

# --------------------------------------------------------------------------------------
# Catalogue des scénarios (injecté aussi dans la table SQL `scenario`)
# --------------------------------------------------------------------------------------
SCENARIOS: Dict[str, dict] = {
    "IDLE":   dict(categorie="SYSTEME", libelle="Surveillance normale", severite="INFO",
                   buzzer=0, ligne1="SENTINEL-X", ligne2="Surveillance OK"),
    "ACC-01": dict(categorie="ACCES", libelle="Membre de l'équipe reconnu et autorisé", severite="INFO",
                   buzzer=0, ligne1="ACCES AUTORISE", ligne2="Bienvenue {nom}"),
    "ACC-02": dict(categorie="ACCES", libelle="Intrusion : visage inconnu", severite="CRITICAL",
                   buzzer=2, ligne1="INTRUSION", ligne2="Personne inconnue"),
    "ACC-03": dict(categorie="ACCES", libelle="Présence détectée, identification en cours", severite="INFO",
                   buzzer=0, ligne1="IDENTIFICATION", ligne2="Regardez la camera"),
    "ACC-04": dict(categorie="ACCES", libelle="Présence non identifiée (aucun visage exploitable)", severite="WARNING",
                   buzzer=1, ligne1="ALERTE PRESENCE", ligne2="Non identifie"),
    "ACC-05": dict(categorie="ACCES", libelle="Visage connu sans signature thermique humaine (usurpation)", severite="CRITICAL",
                   buzzer=2, ligne1="ACCES REFUSE", ligne2="Chaleur absente"),
    "ACC-06": dict(categorie="ACCES", libelle="Visage reconnu mais confiance insuffisante", severite="WARNING",
                   buzzer=1, ligne1="CONFIANCE TROP FAIBLE", ligne2="Taux : {conf}"),
    "ENV-01": dict(categorie="ENVIRONNEMENT", libelle="Anomalie environnementale détectée par l'IA", severite="WARNING",
                   buzzer=1, ligne1="ATTENTION", ligne2="{env}"),
    "ENV-02": dict(categorie="ENVIRONNEMENT", libelle="Danger environnemental critique", severite="CRITICAL",
                   buzzer=2, ligne1="ALERTE DANGER", ligne2="{env}"),
    # Prévisions météo (meteo_risque.py) : consignées dans le journal, jamais envoyées à l'ESP
    "MET-01": dict(categorie="ENVIRONNEMENT", libelle="Vigilance météo : événement notable prévu", severite="WARNING",
                   buzzer=0, ligne1="METEO", ligne2="Vigilance"),
    "MET-02": dict(categorie="ENVIRONNEMENT", libelle="Danger météo : événement fort prévu", severite="CRITICAL",
                   buzzer=0, ligne1="METEO", ligne2="Danger"),
}

ENV_TEXTES = {
    "gaz": "Fuite de gaz",
    "incendie": "Fumee / incendie",
    "surchauffe": "Surchauffe",
    "humidite": "Humidite anormal",
    "derive": "Derive suspecte",
    "meteo_vent": "Vent violent",          # alertes météo (meteo_risque.py), même chemin que les capteurs
    "meteo_pluie": "Fortes pluies",
    "meteo_chaleur": "Forte chaleur",
    "meteo_froid": "Grand froid",
    "meteo_ia": "Risque meteo IA",
}
ENV_TEXTE_DEFAUT = "Anomalie capteur"      # <= 16 caractères : l'écran LCD de l'ESP fait 16 colonnes

# Priorité d'affichage OLED quand plusieurs scénarios sont actifs (le plus prioritaire d'abord)
PRIORITE_ECRAN = ["ENV-02", "ACC-02", "ACC-05", "ACC-04", "ACC-06", "ENV-01", "ACC-01", "ACC-03", "IDLE"]
# Scénarios consignés dans le journal `evenement` (ACC-03 et IDLE sont transitoires)
CODES_JOURNALISES = {"ACC-01", "ACC-02", "ACC-04", "ACC-05", "ACC-06", "ENV-01", "ENV-02"}


# --------------------------------------------------------------------------------------
# Structures de données
# --------------------------------------------------------------------------------------
@dataclass
class Capteurs:
    """Dernier message `sentinel/sensors` (voir MQTT_SCHEMA.md)."""
    temperature: Optional[float] = None
    humidite: Optional[float] = None
    gaz: Optional[float] = None          # MQ-2 : gaz combustibles ET fumées (une seule voie analogique)
    presence: int = 0                    # PIR HC-SR501
    ir_temp: Optional[float] = None      # température infrarouge de la cible (°C), optionnelle
    rfid_uid: Optional[str] = None
    ts: float = field(default_factory=time.time)


@dataclass
class Vision:
    """Dernier message `sentinel/vision` publié par le script caméra."""
    label: Optional[str] = None          # identité renvoyée par le modèle (ou "inconnu")
    confiance: float = 0.0
    connu: bool = False                  # le modèle IA dit : "membre de l'équipe"
    visages: int = 0                     # nombre de visages vus dans l'image
    ts: float = field(default_factory=time.time)


@dataclass
class EnvResult:
    """Sortie de l'IA environnementale (voir analytics.py)."""
    niveau: int = 0                      # 0 normal / 1 anomalie / 2 critique
    score: float = 0.0                   # 0..1
    categorie: str = "aucune"            # gaz | incendie | surchauffe | humidite | derive
    raison: str = ""
    modele: str = ""


@dataclass
class Evenement:
    code: str
    severite: str
    ligne1: str
    ligne2: str
    label_ia: Optional[str] = None
    nom: Optional[str] = None
    confiance: Optional[float] = None
    categorie_env: Optional[str] = None
    score_ia: Optional[float] = None
    modele_ia: Optional[str] = None
    instantane: dict = field(default_factory=dict)


@dataclass
class Decision:
    buzzer: int = 0                      # 0 off / 1 intermittent / 2 continu rapide
    ligne1: str = "SENTINEL-X"
    ligne2: str = "Surveillance OK"
    scenarios: List[str] = field(default_factory=lambda: ["IDLE"])
    nom: Optional[str] = None
    evenements: List[Evenement] = field(default_factory=list)

    def commande(self) -> dict:
        """Payload publié sur `sentinel/commandes` (lu par le firmware ESP8266)."""
        return {"buzzer": self.buzzer, "ligne1": self.ligne1, "ligne2": self.ligne2}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["commande"] = self.commande()
        return d


@dataclass
class _Acces:
    code: str
    label: Optional[str] = None
    nom: Optional[str] = None
    confiance: Optional[float] = None


def ecran(texte: str, n: int = ECRAN_COLS) -> str:
    """L'OLED n'affiche pas les accents : on normalise en ASCII et on tronque."""
    s = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    return s[:n]


# --------------------------------------------------------------------------------------
# Moteur
# --------------------------------------------------------------------------------------
class MoteurScenarios:
    def __init__(self):
        self._presence_depuis: Optional[float] = None
        self._maintien: Optional[tuple] = None        # (acces, expire_a)
        self._cles_actives: set = set()
        self._dernier_emit: Dict[tuple, float] = {}

    # ---- helpers -------------------------------------------------------------------
    @staticmethod
    def _chaleur_humaine(c: Capteurs) -> Optional[bool]:
        """True/False si un capteur IR renseigne, None si l'info n'existe pas."""
        if c.ir_temp is None:
            return None
        return IR_HUMAIN_MIN <= c.ir_temp <= IR_HUMAIN_MAX

    def _acces(self, c: Optional[Capteurs], v: Optional[Vision], membres: dict,
               now: float) -> Optional[_Acces]:
        presence = c is not None and c.presence == 1
        if not presence:
            self._presence_depuis = None
            if self._maintien and now < self._maintien[1]:
                return self._maintien[0]              # on garde le message d'accueil quelques secondes
            self._maintien = None
            return None

        if self._presence_depuis is None:
            self._presence_depuis = now
        chaleur = self._chaleur_humaine(c)
        visage = v is not None and (now - v.ts) <= VISION_TTL_S and v.visages > 0

        if visage:
            fiche = membres.get(v.label) if v.label else None
            actif = fiche["actif"] if fiche else True          # inconnu en base mais "connu" du modèle -> auto-enregistré
            if v.connu and actif and v.confiance < CONFIANCE_MIN:
                self._maintien = None                    # reconnu par le modèle, mais pas assez sûr : pas d'accès
                return _Acces("ACC-06", v.label, None, v.confiance)
            connu = v.connu and v.confiance >= CONFIANCE_MIN and actif
            if connu:
                nom = v.label                            # l'écran affiche le label renvoyé par la reconnaissance faciale
                if chaleur is False:
                    self._maintien = None
                    return _Acces("ACC-05", v.label, nom, v.confiance)
                acces = _Acces("ACC-01", v.label, nom, v.confiance)
                if chaleur is True and v.ts - c.ts > FRAICHEUR_IR_S:
                    # La chaleur mesurée date d'avant ce visage : on attend la prochaine trame de l'ESP
                    # au lieu d'autoriser sur une mesure périmée.
                    if self._maintien and self._maintien[0].label == v.label and now < self._maintien[1]:
                        return self._maintien[0]
                    return _Acces("ACC-03")
                self._maintien = (acces, now + MAINTIEN_ACCES_S)
                return acces
            self._maintien = None
            return _Acces("ACC-02", v.label, None, v.confiance)

        # Présence sans visage exploitable
        if chaleur is False:
            return None                                  # objet chaud / animal : on ignore
        if self._maintien and now < self._maintien[1]:
            return self._maintien[0]                     # membre qui détourne la tête
        if now - self._presence_depuis < DELAI_IDENTIFICATION_S:
            return _Acces("ACC-03")
        return _Acces("ACC-04")

    @staticmethod
    def _code_env(env: Optional[EnvResult]) -> Optional[str]:
        if env is None or env.niveau <= 0:
            return None
        return "ENV-02" if env.niveau >= 2 else "ENV-01"

    # ---- API principale --------------------------------------------------------------
    def evaluer(self, capteurs: Optional[Capteurs], vision: Optional[Vision],
                env: Optional[EnvResult], membres: Optional[dict] = None,
                now: Optional[float] = None) -> Decision:
        now = time.time() if now is None else now
        membres = membres or {}
        if capteurs is not None and now - capteurs.ts > CAPTEURS_TTL_S:
            capteurs, env = None, None                   # l'ESP ne répond plus : données périmées

        acces = self._acces(capteurs, vision, membres, now)
        code_env = self._code_env(env)

        actifs: List[str] = []
        if acces:
            actifs.append(acces.code)
        if code_env:
            actifs.append(code_env)
        if not actifs:
            actifs = ["IDLE"]

        # Combinaison des actionneurs : buzzer = pire niveau ; écran = priorité
        buzzer = max(SCENARIOS[c]["buzzer"] for c in actifs)
        principal = min(actifs, key=PRIORITE_ECRAN.index)
        modele = SCENARIOS[principal]
        env_txt = ENV_TEXTES.get(env.categorie, ENV_TEXTE_DEFAUT) if env else ""
        nom = acces.nom if acces else None
        conf = f"{round(acces.confiance * 100)}%" if acces and acces.confiance is not None else ""
        fmt = dict(nom=nom or "", env=env_txt, conf=conf)
        ligne1 = ecran(modele["ligne1"].format(**fmt))
        ligne2 = ecran(modele["ligne2"].format(**fmt))

        decision = Decision(buzzer=buzzer, ligne1=ligne1, ligne2=ligne2,
                            scenarios=actifs, nom=nom)

        # Journal : on n'émet un événement qu'à l'apparition d'un scénario (pas à chaque message)
        instantane = {}
        if capteurs:
            instantane = {k: getattr(capteurs, k) for k in
                          ("temperature", "humidite", "gaz", "presence", "ir_temp")}
        cles = set()
        for code in actifs:
            if code not in CODES_JOURNALISES:
                continue
            est_env = code.startswith("ENV")
            # ENV : une seule alerte par niveau (la catégorie peut évoluer pendant l'incident)
            cle = (code, "" if est_env else (acces.label or ""))
            cles.add(cle)
            if cle in self._cles_actives:
                continue
            if now - self._dernier_emit.get(cle, -1e9) < COOLDOWN_EVENEMENT_S:
                continue
            self._dernier_emit[cle] = now
            m = SCENARIOS[code]
            decision.evenements.append(Evenement(
                code=code, severite=m["severite"],
                ligne1=ecran(m["ligne1"].format(**fmt)), ligne2=ecran(m["ligne2"].format(**fmt)),
                label_ia=None if est_env else acces.label,
                nom=None if est_env else acces.nom,
                confiance=None if est_env else acces.confiance,
                categorie_env=env.categorie if est_env else None,
                score_ia=env.score if est_env else None,
                modele_ia=env.modele if est_env else None,
                instantane=instantane))
        self._cles_actives = cles
        return decision
