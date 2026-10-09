"""
IA prédictive de risque météo extrême pour SENTINEL-X (onglet « Prévisions »).

Deux niveaux d'analyse, volontairement complémentaires :

  1. VIGILANCE (règles) : les prévisions horaires d'Open-Meteo sur 72 h sont comparées à des seuils
     (rafales, cumul de pluie sur 6 h, chaleur, froid). Explicable : on sait toujours « pourquoi ».
  2. IA (apprentissage) : un modèle (HistGradientBoosting, scikit-learn) est ENTRAÎNÉ sur ~4 ans d'historique
     réel (réanalyse ERA5, archive Open-Meteo) du lieu choisi. Il apprend, à partir de l'état de l'atmosphère
     (pression et sa chute sur 3 h / 12 h, humidité, vent, pluie récente, saison…), la probabilité qu'un
     événement notable survienne dans les 24 h suivantes. Il est évalué sur les derniers 20 % de la période
     (jamais vus à l'entraînement) : l'AUC mesurée est affichée dans le front.

L'IA est appliquée à chaque heure de la prévision : on obtient une courbe de probabilité sur 72 h.

Limites assumées : modèle propre à un lieu (réentraîné à chaque changement de lieu), événements « notables »
(niveau vigilance jaune, pas des catastrophes rarissimes), prévision issue d'un modèle numérique tiers.
Ce n'est PAS un système d'alerte officiel : en cas de danger réel, suivre Météo-France / les autorités.

Aucune dépendance nouvelle : urllib (HTTP), numpy et scikit-learn (déjà dans requirements.txt).
"""
import json
import os
import pickle
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np

API_PREVISION = "https://api.open-meteo.com/v1/forecast"
API_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
API_GEOCODAGE = "https://geocoding-api.open-meteo.com/v1/search"
VARIABLES = ["temperature_2m", "relative_humidity_2m", "precipitation", "pressure_msl",
             "cloud_cover", "wind_speed_10m", "wind_gusts_10m"]

HORIZON_PREVISION_H = 72          # fenêtre affichée / analysée
HORIZON_IA_H = 24                 # l'IA prédit « événement dans les 24 h suivantes »
ANNEES_HISTORIQUE = 4
DELAI_ARCHIVE_J = 6               # l'archive ERA5 a ~5 jours de retard
# Alarme (écran + buzzer de l'ESP) : déclenchée si le danger est prévu dans les ALARME_HEURES prochaines heures.
# Plus loin (jusqu'à 72 h) il reste affiché et consigné, mais le buzzer ne sonne pas des jours entiers.
ALARME_HEURES = int(os.getenv("METEO_ALARME_H", "24"))
ALARME_PEREMPTION_S = 3 * 3600.0     # résultat trop ancien (API injoignable) : on n'alarme plus sur lui
DUREE_CACHE_S = 900.0             # on ne rappelle l'API que toutes les 15 min
MIN_EXEMPLES_PAR_CLASSE = 50

# (niveau 1, niveau 2) : jaune / orange environ. « froid » est testé sur -température.
SEUILS = {"vent": (60.0, 90.0), "pluie": (20.0, 40.0), "chaleur": (35.0, 40.0), "froid": (5.0, 15.0)}
LIBELLES = {"vent": "Vent violent (rafales)", "pluie": "Fortes pluies (cumul 6 h)",
            "chaleur": "Forte chaleur", "froid": "Grand froid"}
UNITES = {"vent": "km/h", "pluie": "mm", "chaleur": "°C", "froid": "°C"}

# Lieu par défaut (modifiable depuis le front ou METEO_NOM / METEO_LAT / METEO_LON)
LIEU_DEFAUT = {"nom": os.getenv("METEO_NOM", "Paris"), "lat": float(os.getenv("METEO_LAT", "48.8566")),
               "lon": float(os.getenv("METEO_LON", "2.3522")), "pays": ""}


class MeteoErreur(Exception):
    pass


# ----------------------------------------------------------------------------------- API Open-Meteo
def _get_json(url, params, timeout=30):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "SENTINEL-X (projet EPSI)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise MeteoErreur(f"API météo injoignable ({type(e).__name__}) : {e}") from e


def _combler(a, defaut=None):
    """Remplace les valeurs manquantes (None/NaN) : par `defaut` si fourni, sinon par la dernière valeur connue."""
    ok = ~np.isnan(a)
    if not ok.any():
        return np.zeros_like(a)
    if defaut is not None:
        return np.where(ok, a, defaut)
    idx = np.where(ok, np.arange(len(a)), 0)
    np.maximum.accumulate(idx, out=idx)
    b = a[idx]
    premier = int(np.argmax(ok))
    b[:premier] = b[premier]
    return b


def lire_serie(js):
    """JSON Open-Meteo -> {"time": [...], variable: ndarray}. Heures en UTC (timezone=UTC demandé)."""
    h = (js or {}).get("hourly") or {}
    temps = h.get("time") or []
    if len(temps) < 48:
        raise MeteoErreur("réponse de l'API météo vide ou trop courte")
    serie = {"time": temps}
    for v in VARIABLES:
        brut = h.get(v)
        if brut is None or len(brut) != len(temps):
            raise MeteoErreur(f"variable « {v} » absente de la réponse")
        a = np.array([np.nan if x is None else x for x in brut], dtype=float)
        serie[v] = _combler(a, defaut=0.0 if v == "precipitation" else None)
    return serie


def prevision(lat, lon):
    return lire_serie(_get_json(API_PREVISION, {
        "latitude": lat, "longitude": lon, "hourly": ",".join(VARIABLES), "past_days": 3,
        "forecast_days": 4, "timezone": "UTC"}))


def archive(lat, lon, debut, fin):
    return lire_serie(_get_json(API_ARCHIVE, {
        "latitude": lat, "longitude": lon, "hourly": ",".join(VARIABLES), "start_date": debut.isoformat(),
        "end_date": fin.isoformat(), "timezone": "UTC"}, timeout=90))


def geocoder(nom):
    js = _get_json(API_GEOCODAGE, {"name": nom, "count": 1, "language": "fr", "format": "json"})
    r = (js.get("results") or [None])[0]
    if not r:
        raise MeteoErreur(f"Lieu introuvable : « {nom} »")
    return {"nom": r.get("name", nom), "lat": float(r["latitude"]), "lon": float(r["longitude"]),
            "pays": r.get("country", "")}


# ----------------------------------------------------------------------------------- Vigilance (règles)
def _decale(a, k):
    """a[t-k] (les k premières valeurs répètent a[0])."""
    return np.concatenate([np.full(k, a[0]), a[:-k]]) if k > 0 else a.copy()


def _cumul(a, k):
    """Somme glissante des k dernières heures (heure courante incluse)."""
    c = np.cumsum(a)
    avant = np.concatenate([np.zeros(k), c[:-k]]) if k < len(a) else np.zeros(len(a))
    return c - avant


def _niv(x, s1, s2):
    return (x >= s1).astype(int) + (x >= s2).astype(int)


def niveaux_par_danger(S):
    """{danger: niveau 0/1/2 par heure}, et {danger: valeur mesurée par heure}."""
    T, G = S["temperature_2m"], S["wind_gusts_10m"]
    r6 = _cumul(S["precipitation"], 6)
    valeurs = {"vent": G, "pluie": r6, "chaleur": T, "froid": T}
    niveaux = {"vent": _niv(G, *SEUILS["vent"]), "pluie": _niv(r6, *SEUILS["pluie"]),
               "chaleur": _niv(T, *SEUILS["chaleur"]), "froid": _niv(-T, *SEUILS["froid"])}
    return niveaux, valeurs


def vigilance(S, i0, horizon=HORIZON_PREVISION_H):
    """Dangers prévus entre l'heure i0 et i0+horizon : [{type, libelle, niveau, quand, valeur, unite}]."""
    niveaux, valeurs = niveaux_par_danger(S)
    fin = min(len(S["time"]), i0 + horizon + 1)
    dangers = []
    for d, niv in niveaux.items():
        fenetre = niv[i0:fin]
        if fenetre.size == 0 or fenetre.max() < 1:
            continue
        pire = int(fenetre.max())
        j = i0 + int(np.argmax(fenetre == pire))          # première heure où le pire niveau est atteint
        dangers.append({"type": d, "libelle": LIBELLES[d], "niveau": pire, "quand": S["time"][j] + "Z",
                        "valeur": round(float(valeurs[d][j]), 1), "unite": UNITES[d]})
    dangers.sort(key=lambda x: (-x["niveau"], x["quand"]))
    return dangers


# ----------------------------------------------------------------------------------- IA
NOMS_CARACTERISTIQUES = ["temperature", "humidite", "pression", "nuages", "vent", "rafales", "pluie_1h",
                         "chute_pression_3h", "chute_pression_12h", "variation_temp_6h", "pluie_6h",
                         "pluie_24h", "rafales_max_6h", "saison_sin", "saison_cos"]


def caracteristiques(S):
    """Matrice (n_heures, n_caractéristiques) : état de l'atmosphère et tendances à chaque heure."""
    T, H, P, C = S["temperature_2m"], S["relative_humidity_2m"], S["pressure_msl"], S["cloud_cover"]
    W, G, R = S["wind_speed_10m"], S["wind_gusts_10m"], S["precipitation"]
    gmax6 = G.copy()
    for k in range(1, 6):
        gmax6 = np.maximum(gmax6, _decale(G, k))
    jours = np.array([datetime.strptime(t, "%Y-%m-%dT%H:%M").timetuple().tm_yday for t in S["time"]], dtype=float)
    ang = 2 * np.pi * jours / 365.25
    return np.column_stack([T, H, P, C, W, G, R, P - _decale(P, 3), P - _decale(P, 12), T - _decale(T, 6),
                            _cumul(R, 6), _cumul(R, 24), gmax6, np.sin(ang), np.cos(ang)])


def etiquettes(S, horizon=HORIZON_IA_H):
    """y[t] = 1 si un danger de niveau >= 1 survient dans les `horizon` heures SUIVANTES (t exclu)."""
    niveaux, _ = niveaux_par_danger(S)
    ev = (np.max(np.vstack(list(niveaux.values())), axis=0) >= 1).astype(float)
    c = np.cumsum(ev)
    m = len(ev) - horizon
    return ((c[horizon:] - c[:m]) > 0).astype(int)


def entrainer(S):
    """Entraîne et évalue le modèle sur une série historique. Lève MeteoErreur si les données sont insuffisantes."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    y = etiquettes(S)
    X = caracteristiques(S)[:len(y)]
    pos = int(y.sum())
    if pos < MIN_EXEMPLES_PAR_CLASSE or len(y) - pos < MIN_EXEMPLES_PAR_CLASSE:
        raise MeteoErreur(f"trop peu d'événements notables dans l'historique de ce lieu ({pos} cas) pour entraîner l'IA")

    def modele():
        return HistGradientBoostingClassifier(max_iter=150, learning_rate=0.08, max_depth=4, random_state=0)

    coupe = int(len(y) * 0.8)
    auc = None
    test = y[coupe:]
    if 0 < test.sum() < len(test):
        # on laisse 24 h d'écart entre entraînement et test : les étiquettes regardent 24 h dans le futur
        m = modele().fit(X[:coupe - HORIZON_IA_H], y[:coupe - HORIZON_IA_H])
        auc = float(roc_auc_score(test, m.predict_proba(X[coupe:])[:, 1]))
    final = modele().fit(X, y)
    return {"modele": final, "auc": auc, "taux_base": float(y.mean()), "n": int(len(y)), "positifs": pos,
            "entraine_le": time.time()}


def niveau_ia(p, taux_base):
    if p >= 0.6:
        return 2
    return 1 if p >= max(0.3, 2.5 * taux_base) else 0


# ----------------------------------------------------------------------------------- Service
def _iso_vers_epoch(temps):
    return np.array([datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp() for t in temps])


class ServiceMeteo:
    """Cache + entraînement en arrière-plan. `donnees()` ne bloque jamais (renvoie « chargement » au début)."""

    def __init__(self, dossier, fetch_prevision=prevision, fetch_archive=archive, fetch_geocodage=geocoder,
                 horloge=time.time):
        self.dossier = dossier
        try:
            os.makedirs(dossier, exist_ok=True)
        except OSError:
            pass
        self.fichier_lieu = os.path.join(dossier, "meteo_lieu.json")
        self._prev, self._arch, self._geo, self._horloge = fetch_prevision, fetch_archive, fetch_geocodage, horloge
        self._lock = threading.Lock()
        self._lieu = self._charger_lieu()
        self._resultat = None
        self._erreur = None
        self._en_cours = False
        self._modele = None          # dict issu de entrainer()
        self._modele_lieu = None
        self._entrainement = None    # None | "en_cours" | message d'échec
        self._expire = True

    # ---- lieu
    def _charger_lieu(self):
        try:
            with open(self.fichier_lieu, encoding="utf-8") as f:
                d = json.load(f)
            return {"nom": str(d["nom"]), "lat": float(d["lat"]), "lon": float(d["lon"]), "pays": str(d.get("pays", ""))}
        except Exception:
            return dict(LIEU_DEFAUT)

    @property
    def lieu(self):
        return dict(self._lieu)

    def changer_lieu(self, nom):
        nom = (nom or "").strip()
        if not nom or len(nom) > 80:
            raise MeteoErreur("nom de lieu invalide")
        lieu = self._geo(nom)
        with self._lock:
            self._lieu = lieu
            self._resultat, self._erreur, self._modele, self._modele_lieu, self._entrainement = None, None, None, None, None
            self._expire = True
        try:
            with open(self.fichier_lieu, "w", encoding="utf-8") as f:
                json.dump(lieu, f, ensure_ascii=False)
        except OSError:
            pass
        return dict(lieu)

    # ---- modèle (fichier par lieu)
    def _chemin_modele(self, lieu):
        return os.path.join(self.dossier, f"meteo_{lieu['lat']:.2f}_{lieu['lon']:.2f}.pkl")

    def _charger_modele(self, lieu):
        try:
            with open(self._chemin_modele(lieu), "rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    def _entrainer_en_fond(self, lieu):
        try:
            fin = datetime.fromtimestamp(self._horloge(), timezone.utc).date() - timedelta(days=DELAI_ARCHIVE_J)
            debut = fin - timedelta(days=365 * ANNEES_HISTORIQUE)
            print(f"🌦️  Entraînement de l'IA météo pour {lieu['nom']} ({debut} → {fin})…")
            res = entrainer(self._arch(lieu["lat"], lieu["lon"], debut, fin))
            try:
                with open(self._chemin_modele(lieu), "wb") as f:
                    pickle.dump(res, f)
            except OSError:
                pass
            with self._lock:
                if self._lieu == lieu:
                    self._modele, self._modele_lieu, self._entrainement, self._expire = res, lieu, None, True
            auc = f"{res['auc']:.2f}" if res["auc"] is not None else "n/a"
            print(f"🌦️  IA météo prête (AUC test {auc}, {res['positifs']} cas sur {res['n']} h).")
        except Exception as e:
            with self._lock:
                if self._lieu == lieu:
                    self._entrainement = f"{e}"
            print(f"🌦️  IA météo indisponible : {e}")

    # ---- calcul
    def calculer(self):
        """Appel réseau + analyse (bloquant). Utilisé par le thread de fond."""
        lieu = self.lieu
        S = self._prev(lieu["lat"], lieu["lon"])
        maintenant = self._horloge()
        i0 = max(0, int(np.searchsorted(_iso_vers_epoch(S["time"]), maintenant, side="right")) - 1)
        fin = min(len(S["time"]), i0 + HORIZON_PREVISION_H + 1)

        with self._lock:
            modele = self._modele if self._modele_lieu == lieu else None
            demarrer = modele is None and self._entrainement is None
            if demarrer:
                modele = self._charger_modele(lieu)
                if modele is not None:
                    self._modele, self._modele_lieu = modele, lieu
                else:
                    self._entrainement = "en_cours"
            statut = self._entrainement
        if demarrer and modele is None:
            threading.Thread(target=self._entrainer_en_fond, args=(lieu,), daemon=True).start()

        dangers = vigilance(S, i0)
        niv_regles = max([d["niveau"] for d in dangers], default=0)
        proba = None
        if modele is not None:
            proba = modele["modele"].predict_proba(caracteristiques(S))[:, 1]
            fenetre = proba[i0:fin]
            pmax = float(fenetre.max())
            j = i0 + int(np.argmax(fenetre))
            niv_ia = niveau_ia(pmax, modele["taux_base"])
            ia = {"etat": "pret", "niveau": niv_ia, "proba_max": round(pmax, 3), "quand": S["time"][j] + "Z",
                  "taux_base": round(modele["taux_base"], 3), "auc": None if modele["auc"] is None else round(modele["auc"], 3),
                  "exemples": modele["n"], "positifs": modele["positifs"]}
        else:
            niv_ia = 0
            ia = {"etat": "entrainement" if statut == "en_cours" else "indisponible",
                  "message": "Entraînement sur l'historique du lieu en cours…" if statut == "en_cours" else statut}

        T, G = S["temperature_2m"], S["wind_gusts_10m"]
        r6 = _cumul(S["precipitation"], 6)
        serie = [{"t": S["time"][i] + "Z", "temperature": round(float(T[i]), 1), "rafales": round(float(G[i]), 1),
                  "pluie6h": round(float(r6[i]), 1), "proba": None if proba is None else round(float(proba[i]), 3)}
                 for i in range(i0, fin)]
        niveau = max(niv_regles, niv_ia)

        # Alarme : dangers (règles) proches dans le temps ; l'IA seule ne sonne qu'à probabilité forte (>= 0.6)
        proches = vigilance(S, i0, horizon=ALARME_HEURES)
        niv_proche = max([d["niveau"] for d in proches], default=0)
        alarme = {"niveau": 0, "heures": ALARME_HEURES}
        p_proche = float(proba[i0:i0 + ALARME_HEURES + 1].max()) if proba is not None else 0.0
        if niv_proche > 0:
            alarme.update(niveau=niv_proche, categorie="meteo_" + proches[0]["type"], score=niv_proche / 2,
                          raison=f"{proches[0]['libelle']} d'ici {ALARME_HEURES} h")
        elif proba is not None and p_proche >= 0.6:
            alarme.update(niveau=2, categorie="meteo_ia", score=round(p_proche, 3),
                          raison=f"IA : {round(100 * p_proche)} % d'événement notable d'ici {ALARME_HEURES} h")
        return {"etat": "ok", "lieu": lieu, "calcule_le": maintenant, "niveau": niveau,
                "niveau_regles": niv_regles, "alarme": alarme, "dangers": dangers, "ia": ia,
                "actuel": {"temperature": round(float(T[i0]), 1), "humidite": round(float(S["relative_humidity_2m"][i0]), 0),
                           "pression": round(float(S["pressure_msl"][i0]), 1), "vent": round(float(S["wind_speed_10m"][i0]), 1),
                           "rafales": round(float(G[i0]), 1), "pluie_1h": round(float(S["precipitation"][i0]), 1)},
                "serie": serie}

    def alarme(self):
        """Alarme à déclencher maintenant ({niveau, categorie, score, raison}) ou None. Lecture du cache, sans réseau."""
        with self._lock:
            res = self._resultat
        if not res or self._horloge() - res["calcule_le"] > ALARME_PEREMPTION_S:
            return None
        a = res.get("alarme")
        return a if a and a["niveau"] > 0 else None

    def rafraichir(self):
        """Recalcule et met en cache ; renvoie le résultat (ou lève MeteoErreur)."""
        with self._lock:
            if self._en_cours:
                return self._resultat
            self._en_cours = True
        lieu = self.lieu
        try:
            res = self.calculer()
            with self._lock:
                if self._lieu == lieu:
                    self._resultat, self._erreur, self._expire = res, None, False
            return res
        except Exception as e:
            with self._lock:
                self._erreur = str(e)
            raise
        finally:
            with self._lock:
                self._en_cours = False

    def _rafraichir_silencieux(self):
        try:
            self.rafraichir()
        except Exception as e:
            print(f"🌦️  Météo : {e}")

    def donnees(self):
        """Dernier résultat connu, immédiatement. Déclenche un rafraîchissement en fond s'il est périmé."""
        with self._lock:
            res, err, lieu = self._resultat, self._erreur, dict(self._lieu)
            perime = self._expire or res is None or self._horloge() - res["calcule_le"] > DUREE_CACHE_S
            lancer = perime and not self._en_cours
        if lancer:
            threading.Thread(target=self._rafraichir_silencieux, daemon=True).start()
        if res is not None:
            return {**res, "avertissement": err} if err else res
        if err:
            return {"etat": "erreur", "lieu": lieu, "message": err}
        return {"etat": "chargement", "lieu": lieu}
