"""
SENTINEL-X : IA analytique des mesures stockées en base (température, humidité, gaz).

Principe
--------
On relit en base les mesures des 5 dernières minutes (même format JSON que simulator.py :
temperature, humidite, gaz), qu'on regroupe en moyennes PAR MINUTE. Quatre contrôles :

1. Variation minute à minute : une variation régulière est normale, mais une minute qui "saute" par
   rapport à la précédente est anormale. C'est un MODÈLE IA ENTRAÎNÉ SOUS ORANGE (ai/modele_variations.pkcls,
   voir ai/entrainer_variations.py) qui juge la forme de la courbe : 0 normal / 1 anomalie / 2 critique.
   Sans modèle, on retombe sur la règle simple `var_max` (au-delà = CRITIQUE).
2. Limites absolues, sur la mesure la plus récente (tout se règle dans CONFIG ci-dessous) :
   - hors [min, max]               -> CRITIQUE (niveau 2)
   - entre la limite et `alerte_*` -> ANOMALIE (niveau 1) : on s'approche d'une limite
3. Dérive en cours (fenêtre glissante de 60 s) : pente de la température et du gaz par régression linéaire,
   avec sa significativité statistique (t de Student). Repère une montée LENTE mais certaine, ou une montée
   rapide en 10-20 s, bien avant que les moyennes par minute ne bougent. Gaz + température qui montent
   ensemble -> CRITIQUE (incendie).
4. Limites de sécurité absolues (SECURITE), indépendantes de CONFIG : filet toujours actif, même si CONFIG
   est réglé trop large ou si le modèle se trompe.

Le module est pur (pas d'E/S) : `analyser_serie(lignes)` reçoit des dicts {ts, temperature, humidite, gaz}.
`AnalyticsAI.analyser()` va chercher la série en base (ou, si la base est indisponible, dans un tampon
mémoire des 5 dernières minutes) et renvoie un EnvResult pour le moteur de scénarios.
"""
import logging
import os
import pickle
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np

from scenarios import Capteurs, EnvResult

log = logging.getLogger("sentinelx.analytics")

FENETRE_S = 300.0              # laps de temps analysé : 5 minutes
MINUTE_S = 60.0
MIN_POINTS_MINUTE = 3          # une minute avec moins de mesures n'est pas fiable

# --- Dérive : pente sur fenêtre glissante + significativité statistique (t de Student) ---
FENETRE_PENTE_S = 60.0
MIN_POINTS_PENTE = 8
MIN_SPAN_S = 10.0
PLANCHER_SIGMA = {"temperature": 0.1, "gaz": 5.0}   # bruit résiduel minimal supposé (évite une significativité infinie)
PENTE_GAZ_ANOMALIE = 40.0      # unités/min
PENTE_TEMP_ANOMALIE = 1.5      # °C/min
PENTE_GAZ_CORREL = 15.0        # exemple du sujet : hausse lente de température + micro-déviation du gaz
PENTE_TEMP_CORREL = 0.5
Z_FORT = 6.0                   # significativité d'une tendance "quasi certaine"
Z_FAIBLE = 4.0

# --- Limites de sécurité absolues, indépendantes de CONFIG : (mesure, opérateur, valeur, niveau) ---
GAZ_CRITIQUE = 600
GAZ_PREALERTE = 400
TEMP_CRITIQUE = 50.0
TEMP_PREALERTE = 40.0
SECURITE = [
    ("gaz", ">=", GAZ_CRITIQUE, 2), ("temperature", ">=", TEMP_CRITIQUE, 2),
    ("gaz", ">=", GAZ_PREALERTE, 1), ("temperature", ">=", TEMP_PREALERTE, 1),
    ("humidite", "<=", 10.0, 1), ("humidite", ">=", 95.0, 1),
]

# --- Modèle IA des variations (entraîné sous Orange) ---
MODEL_PATH = os.getenv("ANALYTICS_MODEL_PATH",
                       os.path.join(os.path.dirname(__file__), "..", "ai", "modele_variations.pkcls"))
# Variables d'entrée du modèle : 4 par mesure, toutes en % (indépendantes des unités). ORDRE = colonnes du jeu d'entraînement.
STATS = ("var_derniere", "var_max", "var_ecart_type", "ecart_moyenne")
FEATURES = [f"{m}_{st}" for m in ("temperature", "humidite", "gaz") for st in STATS]
CLASSES = {"normal": 0, "anomalie": 1, "critique": 2}

# --------------------------------------------------------------------------------------
# RÉGLAGES PAR MESURE (à modifier ici)
#   min / max            : limites CRITIQUES (None = pas de limite)
#   alerte_min/alerte_max: seuil d'ANOMALIE avant la limite critique (None = pas de pré-alerte)
#   var_max              : variation maximale d'une minute à la suivante, au-delà = CRITIQUE (None = pas de contrôle)
#   var_mode             : "abs" (en unité de la mesure, ex. °C) ou "pct" (en % de la minute précédente)
# --------------------------------------------------------------------------------------
CONFIG = {
    "temperature": dict(unite="°C", min=0.0, max=40.0, alerte_min=10.0, alerte_max=35.0,
                        var_max=3.0, var_mode="abs"),
    "humidite":    dict(unite="%", min=30.0, max=80.0, alerte_min=35.0, alerte_max=65.0,
                        var_max=10.0, var_mode="pct"),
    # Gaz : valeurs PROVISOIRES (à ajuster), à ajuster quand les seuils seront connus.
    "gaz":         dict(unite="", min=None, max=float(GAZ_CRITIQUE), alerte_min=None, alerte_max=float(GAZ_PREALERTE),
                        var_max=10.0, var_mode="pct"),
}
METRIQUES = tuple(CONFIG)


@dataclass
class AnalyseMetrique:
    nom: str
    derniere: Optional[float] = None
    moyenne_5min: Optional[float] = None
    moyennes_minute: List[float] = field(default_factory=list)      # de la plus ancienne à la plus récente
    variations: List[float] = field(default_factory=list)           # variation entre minutes consécutives (unité de var_mode)
    unite_variation: str = ""
    niveau: int = 0
    raisons: List[str] = field(default_factory=list)


def _moyennes_par_minute(points):
    """points = [(ts, valeur)] -> {indice_minute: moyenne}, 0 = minute la plus récente (par rapport à la dernière mesure)."""
    if not points:
        return {}
    t_ref = max(p[0] for p in points)
    seaux: Dict[int, list] = {}
    for ts, v in points:
        k = int((t_ref - ts) // MINUTE_S)
        seaux.setdefault(k, []).append(v)
    return {k: sum(v) / len(v) for k, v in seaux.items() if len(v) >= MIN_POINTS_MINUTE}


def _analyser_metrique(nom: str, lignes: List[dict], regle_variation: bool = True) -> AnalyseMetrique:
    cfg = CONFIG[nom]
    u = cfg["unite"]
    points = sorted((float(l["ts"]), float(l[nom])) for l in lignes if l.get(nom) is not None)
    res = AnalyseMetrique(nom=nom, unite_variation=u if cfg["var_mode"] == "abs" else "%")
    if not points:
        return res
    res.derniere = points[-1][1]
    res.moyenne_5min = round(sum(p[1] for p in points) / len(points), 2)

    moyennes = _moyennes_par_minute(points)
    indices = sorted(moyennes, reverse=True)                      # chronologique (plus ancienne -> plus récente)
    res.moyennes_minute = [round(moyennes[k], 2) for k in indices]

    # 1) variation minute à minute (uniquement entre minutes adjacentes) : au-delà de var_max = CRITIQUE
    for ancien, recent in zip(indices, indices[1:]):
        if ancien - recent != 1:
            continue
        delta = moyennes[recent] - moyennes[ancien]
        if cfg["var_mode"] == "pct":
            if moyennes[ancien] == 0:
                continue
            delta = delta / abs(moyennes[ancien]) * 100.0
        res.variations.append(round(delta, 2))
        if regle_variation and cfg["var_max"] is not None and abs(delta) > cfg["var_max"]:
            res.niveau = 2
            quand = f" (il y a {recent} min)" if recent else ""
            res.raisons.append(f"{nom} {delta:+.1f}{res.unite_variation}/min > {cfg['var_max']:g}{res.unite_variation}{quand}")

    # 2) limites absolues sur la mesure la plus récente
    d = res.derniere
    if cfg["max"] is not None and d >= cfg["max"]:
        res.niveau = 2
        res.raisons.append(f"{nom} {d:.1f}{u} >= max {cfg['max']:g}{u}")
    elif cfg["min"] is not None and d <= cfg["min"]:
        res.niveau = 2
        res.raisons.append(f"{nom} {d:.1f}{u} <= min {cfg['min']:g}{u}")
    elif cfg["alerte_max"] is not None and d >= cfg["alerte_max"]:
        res.niveau = max(res.niveau, 1)
        res.raisons.append(f"{nom} {d:.1f}{u} proche du max ({cfg['alerte_max']:g}{u})")
    elif cfg["alerte_min"] is not None and d <= cfg["alerte_min"]:
        res.niveau = max(res.niveau, 1)
        res.raisons.append(f"{nom} {d:.1f}{u} proche du min ({cfg['alerte_min']:g}{u})")
    return res


def _pente_z(points, plancher):
    """Pente (unité/min) de la régression linéaire sur `points` [(ts, valeur)] + significativité (pente / erreur-type).

    La significativité distingue une vraie tendance (même lente) d'un simple bruit de mesure.
    """
    n = len(points)
    if n < MIN_POINTS_PENTE or points[-1][0] - points[0][0] < MIN_SPAN_S:
        return 0.0, 0.0
    t = np.asarray([p[0] for p in points], dtype=float)
    t -= t[0]
    y = np.asarray([p[1] for p in points], dtype=float)
    coef = np.polyfit(t, y, 1)
    residus = y - np.polyval(coef, t)
    sigma = max(float(np.sqrt(np.sum(residus ** 2) / (n - 2))), plancher)
    se = sigma / float(np.sqrt(np.sum((t - t.mean()) ** 2)))
    return float(coef[0] * 60.0), float(coef[0] / se)


def _pentes(lignes: List[dict]) -> Dict[str, tuple]:
    """{mesure: (pente/min, significativité)} sur les FENETRE_PENTE_S dernières secondes."""
    out = {}
    for nom in ("temperature", "gaz"):
        pts = sorted((float(l["ts"]), float(l[nom])) for l in lignes if l.get(nom) is not None)
        if pts:
            pts = [p for p in pts if pts[-1][0] - p[0] <= FENETRE_PENTE_S]
        out[nom] = _pente_z(pts, PLANCHER_SIGMA[nom])
    return out


def _appliquer_derives(par_metrique: Dict[str, AnalyseMetrique], pentes: Dict[str, tuple]):
    """Dérive de la température et/ou du gaz -> niveau + raison sur les mesures concernées."""
    (pt, zt), (pg, zg) = pentes["temperature"], pentes["gaz"]
    derive_gaz = pg >= PENTE_GAZ_ANOMALIE and zg >= Z_FORT
    derive_temp = pt >= PENTE_TEMP_ANOMALIE and zt >= Z_FORT
    correl = pt >= PENTE_TEMP_CORREL and pg >= PENTE_GAZ_CORREL and zt >= Z_FAIBLE and zg >= Z_FAIBLE
    if derive_gaz and derive_temp:
        niveau, cibles = 2, ("temperature", "gaz")
        raison = f"hausse simultanée gaz (+{pg:.0f}/min) et température (+{pt:.1f}°C/min)"
    elif derive_gaz:
        niveau, cibles, raison = 1, ("gaz",), f"dérive du gaz (+{pg:.0f}/min, signif.={zg:.1f})"
    elif derive_temp:
        niveau, cibles, raison = 1, ("temperature",), f"dérive de température (+{pt:.1f}°C/min, signif.={zt:.1f})"
    elif correl:
        niveau, cibles = 1, ("temperature", "gaz")
        raison = f"corrélation température/gaz suspecte (+{pt:.1f}°C/min, +{pg:.0f}/min)"
    else:
        return
    for nom in cibles:
        par_metrique[nom].niveau = max(par_metrique[nom].niveau, niveau)
    par_metrique[cibles[0]].raisons.append(raison)


def _appliquer_securite(par_metrique: Dict[str, AnalyseMetrique]):
    """Limites absolues : jamais moins sûr que ces valeurs, quel que soit CONFIG ou le modèle."""
    for nom, op, seuil, niveau in SECURITE:
        a = par_metrique[nom]
        d = a.derniere
        if d is None or niveau <= a.niveau:
            continue
        if (op == ">=" and d >= seuil) or (op == "<=" and d <= seuil):
            a.niveau = niveau
            a.raisons.append(f"limite de sécurité : {nom} {d:.1f} {op} {seuil:g}")


def _categorie(par_metrique: Dict[str, AnalyseMetrique], pentes: Dict[str, tuple]) -> str:
    anormal = {n for n, a in par_metrique.items() if a.niveau > 0}
    t = par_metrique["temperature"]
    cfg = CONFIG["temperature"]
    seuil_chaud = min([x for x in (cfg["alerte_max"], cfg["max"], TEMP_PREALERTE) if x is not None])
    chaud = t.niveau > 0 and ((t.derniere is not None and t.derniere >= seuil_chaud)
                              or pentes["temperature"][0] >= PENTE_TEMP_ANOMALIE)
    if "gaz" in anormal and chaud:
        return "incendie"
    if "gaz" in anormal:
        return "gaz"
    if chaud:
        return "surchauffe"
    if "temperature" in anormal:
        return "derive"
    if "humidite" in anormal:
        return "humidite"
    return "aucune"


def _variations_pct(lignes: List[dict], nom: str) -> List[float]:
    """Variations (%) entre moyennes de minutes consécutives, de la plus ancienne à la plus récente."""
    points = [(float(l["ts"]), float(l[nom])) for l in lignes if l.get(nom) is not None]
    moy = _moyennes_par_minute(points)
    out = []
    for ancien, recent in zip(sorted(moy, reverse=True), sorted(moy, reverse=True)[1:]):
        if ancien - recent == 1 and moy[ancien] != 0:
            out.append((moy[recent] - moy[ancien]) / abs(moy[ancien]) * 100.0)
    return out


def extraire_features(lignes: List[dict]) -> Dict[str, float]:
    """Variables d'entrée du modèle IA (identiques à l'entraînement : ai/generer_dataset_variations.py)."""
    feats: Dict[str, float] = {}
    for nom in METRIQUES:
        v = _variations_pct(lignes, nom)
        vals = [float(l[nom]) for l in lignes if l.get(nom) is not None]
        moyenne = sum(vals) / len(vals) if vals else 0.0
        feats[f"{nom}_var_derniere"] = v[-1] if v else 0.0
        feats[f"{nom}_var_max"] = max(v, key=abs) if v else 0.0
        feats[f"{nom}_var_ecart_type"] = float(np.std(v)) if v else 0.0
        feats[f"{nom}_ecart_moyenne"] = (vals[-1] - moyenne) / abs(moyenne) * 100.0 if vals and moyenne else 0.0
    return feats


def predire_niveau(modele, feats: Dict[str, float]) -> int:
    """Interroge le modèle (Orange .pkcls ou tout objet scikit-learn) -> 0 normal / 1 anomalie / 2 critique."""
    domain = getattr(modele, "domain", None)
    if domain is not None:                                         # modèle Orange : on respecte l'ordre de SES colonnes
        x = np.asarray([[feats[a.name] for a in domain.attributes]], dtype=float)
        idx = int(np.asarray(modele(x)).ravel()[0])
        nom = str(domain.class_var.values[idx])
        return CLASSES.get(nom, int(nom) if nom.isdigit() else idx)
    y = modele.predict(np.asarray([[feats[f] for f in FEATURES]], dtype=float))
    return max(0, min(2, int(np.asarray(y).ravel()[0])))


def analyser_serie(lignes: List[dict], modele=None) -> dict:
    """Analyse pure d'une série de mesures [{ts, temperature, humidite, gaz}] (ts en secondes).

    Avec `modele`, l'IA juge les variations ; sans (ou si elle plante), règle simple `var_max`.
    """
    feats = extraire_features(lignes)
    niveau_ia = None
    if modele is not None:
        try:
            niveau_ia = predire_niveau(modele, feats)
        except Exception as e:
            log.error("Modèle des variations en échec (%s) -> règle simple.", e)
    par_metrique = {n: _analyser_metrique(n, lignes, regle_variation=niveau_ia is None) for n in METRIQUES}

    if niveau_ia:                       # l'IA ne dit pas QUELLE mesure : on désigne celle qui varie le plus
        coupable = max(METRIQUES, key=lambda n: abs(feats[f"{n}_var_max"]))
        a = par_metrique[coupable]
        a.niveau = max(a.niveau, niveau_ia)
        a.raisons.append(f"variation anormale (IA) : {coupable} {feats[f'{coupable}_var_max']:+.1f}%/min")

    pentes = _pentes(lignes)
    _appliquer_derives(par_metrique, pentes)
    _appliquer_securite(par_metrique)

    niveau = max(a.niveau for a in par_metrique.values())
    raisons = [r for a in par_metrique.values() for r in a.raisons]
    nb = sum(1 for a in par_metrique.values() if a.niveau > 0)
    score = 0.0 if niveau == 0 else min(1.0, (0.5 if niveau == 1 else 0.9) + 0.1 * (nb - 1))
    return {"niveau": niveau, "score": round(score, 3), "categorie": _categorie(par_metrique, pentes),
            "raison": " ; ".join(raisons), "nb_mesures": len(lignes),
            "pentes": {n: {"par_min": round(p, 2), "signif": round(z, 1)} for n, (p, z) in pentes.items()},
            "methode_variation": "ia" if niveau_ia is not None else "regle",
            "features": {k: round(v, 2) for k, v in feats.items()},
            "metriques": {n: asdict(a) for n, a in par_metrique.items()}}


class AnalyticsAI:
    def __init__(self, fenetre_s: float = FENETRE_S, model_path: str = MODEL_PATH):
        self.fenetre_s = fenetre_s
        self.dernier: Optional[dict] = None
        self._tampon = deque()          # 5 dernières minutes en mémoire : secours si la base est indisponible
        self.modele = None
        self.nom = "analytics-5min"
        try:
            with open(model_path, "rb") as f:
                self.modele = pickle.load(f)
            self.nom += "+" + os.path.basename(model_path)
            log.info("Modèle IA des variations chargé : %s", model_path)
        except FileNotFoundError:
            log.warning("Modèle des variations absent (%s) : règle simple var_max utilisée.", model_path)
        except Exception as e:                                       # pickle corrompu, Orange absent...
            log.error("Chargement du modèle des variations impossible (%s) : règle simple utilisée.", e)

    def _lignes(self, c: Optional[Capteurs], now: float) -> List[dict]:
        if c is not None:
            self._tampon.append(dict(ts=now, temperature=c.temperature, humidite=c.humidite, gaz=c.gaz))
        while self._tampon and now - self._tampon[0]["ts"] > self.fenetre_s:
            self._tampon.popleft()
        try:
            import db                       # import tardif : l'analyse pure reste testable sans psycopg2
            lignes = db.get_recent_series(self.fenetre_s)
        except Exception as e:
            log.error("Lecture de la base impossible (%s)", e)
            lignes = []
        if not lignes:
            lignes = list(self._tampon)     # base indisponible : on analyse ce qu'on a reçu en mémoire
        return lignes

    def analyser(self, c: Optional[Capteurs] = None, now: Optional[float] = None) -> EnvResult:
        """Relit la base (5 min), analyse, et renvoie un EnvResult. `c` = dernière mesure reçue (secours mémoire)."""
        now = time.time() if now is None else now
        self.dernier = analyser_serie(self._lignes(c, now), self.modele)
        d = self.dernier
        if d["niveau"] == 0:
            return EnvResult(0, 0.0, "aucune", "", self.nom)
        return EnvResult(d["niveau"], d["score"], d["categorie"], d["raison"], self.nom)
