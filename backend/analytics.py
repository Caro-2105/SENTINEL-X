"""
SENTINEL-X : IA analytique des mesures stockées en base (température, humidité, gaz).

Principe
--------
On relit en base les mesures des 5 dernières minutes (même format JSON que simulator.py :
temperature, humidite, gaz), qu'on regroupe en moyennes PAR MINUTE. Deux contrôles :

1. Variation minute à minute : une variation régulière est normale, mais UNE minute qui varie de plus
   que `var_max` par rapport à la précédente est CRITIQUE (niveau 2).
2. Limites absolues, sur la mesure la plus récente (tout se règle dans CONFIG ci-dessous) :
   - hors [min, max]               -> CRITIQUE (niveau 2)
   - entre la limite et `alerte_*` -> ANOMALIE (niveau 1) : on s'approche d'une limite

Le module est pur (pas d'E/S) : `analyser_serie(lignes)` reçoit des dicts {ts, temperature, humidite, gaz}.
`AnalyticsAI.analyser()` va chercher la série en base et renvoie un EnvResult, fusionnable avec celui d'env_ai.
"""
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from env_ai import GAZ_CRITIQUE, GAZ_PREALERTE
from scenarios import EnvResult

log = logging.getLogger("sentinelx.analytics")

FENETRE_S = 300.0              # laps de temps analysé : 5 minutes
MINUTE_S = 60.0
MIN_POINTS_MINUTE = 3          # une minute avec moins de mesures n'est pas fiable

# --------------------------------------------------------------------------------------
# RÉGLAGES PAR MESURE (à modifier ici)
#   min / max            : limites CRITIQUES (None = pas de limite)
#   alerte_min/alerte_max: seuil d'ANOMALIE avant la limite critique (None = pas de pré-alerte)
#   var_max              : variation maximale d'une minute à la suivante, au-delà = CRITIQUE (None = pas de contrôle)
#   var_mode             : "abs" (en unité de la mesure, ex. °C) ou "pct" (en % de la minute précédente)
# --------------------------------------------------------------------------------------
CONFIG = {
    "temperature": dict(unite="°C", min=15.0, max=25.0, alerte_min=16.0, alerte_max=24.0,
                        var_max=1.0, var_mode="abs"),
    "humidite":    dict(unite="%", min=30.0, max=75.0, alerte_min=35.0, alerte_max=70.0,
                        var_max=10.0, var_mode="pct"),
    # Gaz : valeurs PROVISOIRES (reprises de env_ai.py), à ajuster quand les seuils seront connus.
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


def _analyser_metrique(nom: str, lignes: List[dict]) -> AnalyseMetrique:
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
        if cfg["var_max"] is not None and abs(delta) > cfg["var_max"]:
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


def _categorie(par_metrique: Dict[str, AnalyseMetrique]) -> str:
    anormal = {n for n, a in par_metrique.items() if a.niveau > 0}
    t = par_metrique["temperature"]
    chaud = t.niveau > 0 and t.derniere is not None and t.derniere >= CONFIG["temperature"]["alerte_max"]
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



def analyser_serie(lignes: List[dict]) -> dict:
    """Analyse pure d'une série de mesures [{ts, temperature, humidite, gaz}] (ts en secondes)."""
    par_metrique = {n: _analyser_metrique(n, lignes) for n in METRIQUES}
    niveau = max(a.niveau for a in par_metrique.values())
    raisons = [r for a in par_metrique.values() for r in a.raisons]
    nb = sum(1 for a in par_metrique.values() if a.niveau > 0)
    score = 0.0 if niveau == 0 else min(1.0, (0.5 if niveau == 1 else 0.9) + 0.1 * (nb - 1))
    return {"niveau": niveau, "score": round(score, 3), "categorie": _categorie(par_metrique),
            "raison": " ; ".join(raisons), "nb_mesures": len(lignes),
            "metriques": {n: asdict(a) for n, a in par_metrique.items()}}


def fusionner(a: Optional[EnvResult], b: Optional[EnvResult]) -> Optional[EnvResult]:
    """Garde le pire des deux diagnostics environnementaux (le moins sûr l'emporte)."""
    if a is None or b is None:
        return a or b
    if a.niveau == 0 and b.niveau == 0:
        return a
    haut, bas = (a, b) if (a.niveau, a.score) >= (b.niveau, b.score) else (b, a)
    if bas.niveau == 0:
        return haut
    return EnvResult(haut.niveau, max(a.score, b.score), haut.categorie,
                     f"{haut.raison} + {bas.raison}", f"{haut.modele}+{bas.modele}")


class AnalyticsAI:
    modele = "analytics-5min"

    def __init__(self, fenetre_s: float = FENETRE_S):
        self.fenetre_s = fenetre_s
        self.dernier: Optional[dict] = None

    def analyser(self) -> Optional[EnvResult]:
        """Relit la base, analyse, et renvoie un EnvResult (None si la base est indisponible)."""
        try:
            import db                       # import tardif : l'analyse pure reste testable sans psycopg2
            lignes = db.get_recent_series(self.fenetre_s)
        except Exception as e:
            log.error("Analyse analytique impossible (%s)", e)
            return None
        self.dernier = analyser_serie(lignes)
        d = self.dernier
        if d["niveau"] == 0:
            return EnvResult(0, 0.0, "aucune", "", self.modele)
        return EnvResult(d["niveau"], d["score"], d["categorie"], d["raison"], self.modele)
