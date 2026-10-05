"""
SENTINEL-X : analyse IA des risques environnementaux (température, humidité, gaz/fumée).

Ce module est le POINT D'ENCHÂSSEMENT de la future IA environnementale.

Contrat avec le modèle (à respecter par la personne qui entraîne l'IA)
----------------------------------------------------------------------
Fichier  : ai/modele_environnement.pkl   (chemin surchargeable via ENV_MODEL_PATH)
Entrée   : vecteur de 5 variables, dans cet ordre (voir FEATURES) :
           [temperature, humidite, gaz, pente_temperature (°C/min), pente_gaz (unités/min)]
Sortie   : - ENV_MODEL_TYPE=classifieur (défaut) : model.predict(X) -> 0 normal / 1 anomalie / 2 critique
           - ENV_MODEL_TYPE=anomalie (ex. IsolationForest) : predict -> -1 anomalie, 1 normal
             (anomalie => niveau 1 ; le niveau 2 reste décidé par les limites de sécurité)

Tant que le fichier n'existe pas (ou qu'il plante), l'analyseur travaille avec une BASELINE
statistique (pentes + écarts à la normale glissante), et non avec un simple "if temp > 40".
Les limites matérielles dures (gaz >= 600, temp >= 50 °C...) restent TOUJOURS actives en filet
de sécurité : l'IA détecte plus tôt, mais ne peut pas rendre le système moins sûr.
"""
import logging
import os
import pickle
from collections import deque
from typing import Optional

import numpy as np

from scenarios import Capteurs, EnvResult

log = logging.getLogger("sentinelx.env_ai")

MODEL_PATH = os.getenv("ENV_MODEL_PATH",
                       os.path.join(os.path.dirname(__file__), "..", "ai", "modele_environnement.pkl"))
MODEL_TYPE = os.getenv("ENV_MODEL_TYPE", "classifieur")
FEATURES = ["temperature", "humidite", "gaz", "pente_temperature", "pente_gaz"]

# --- Fenêtre glissante ---
FENETRE_S = 60.0
MIN_POINTS = 8
MIN_SPAN_S = 10.0
PLANCHER_SIGMA_TEMP = 0.1     # bruit résiduel minimal supposé (évite une significativité infinie sur un signal parfait)
PLANCHER_SIGMA_GAZ = 5.0

# --- Baseline : seuils sur les pentes (par minute) et sur leur significativité statistique (t de Student) ---
PENTE_GAZ_ANOMALIE = 40.0
PENTE_TEMP_ANOMALIE = 1.5
PENTE_GAZ_CORREL = 15.0       # exemple du sujet : hausse lente de température + micro-déviation de gaz
PENTE_TEMP_CORREL = 0.5
Z_FORT = 6.0                  # significativité de la pente ("tendance quasi certaine")
Z_FAIBLE = 4.0

# --- Limites de sécurité absolues (filet de sécurité, toujours actives) ---
GAZ_CRITIQUE = 600
GAZ_PREALERTE = 400
TEMP_CRITIQUE = 50.0
TEMP_PREALERTE = 40.0
HUM_BASSE = 10.0
HUM_HAUTE = 95.0


def _pente_z(ts, vals, plancher):
    """Pente (unité/min) de la régression linéaire sur la fenêtre + significativité (pente / erreur-type).

    La significativité distingue une vraie tendance (même lente) d'un simple bruit de mesure :
    c'est elle qui permet de repérer une dérive lente sans déclencher sur les fluctuations normales.
    """
    n = len(vals)
    if n < MIN_POINTS or ts[-1] - ts[0] < MIN_SPAN_S:
        return 0.0, 0.0
    t = np.asarray(ts, dtype=float) - ts[0]
    y = np.asarray(vals, dtype=float)
    coef = np.polyfit(t, y, 1)
    residus = y - np.polyval(coef, t)
    sigma = max(float(np.sqrt(np.sum(residus ** 2) / (n - 2))), plancher)
    se = sigma / float(np.sqrt(np.sum((t - t.mean()) ** 2)))
    return float(coef[0] * 60.0), float(coef[0] / se)


def _categorie(temp, gaz, pente_temp, pente_gaz):
    chaud = (temp is not None and temp >= TEMP_PREALERTE) or pente_temp >= PENTE_TEMP_ANOMALIE
    gazeux = (gaz is not None and gaz >= GAZ_PREALERTE) or pente_gaz >= PENTE_GAZ_ANOMALIE
    if chaud and gazeux:
        return "incendie"
    if gazeux:
        return "gaz"
    if chaud:
        return "surchauffe"
    return "derive"


class EnvAnalyzer:
    def __init__(self, model_path: str = MODEL_PATH):
        self.fenetre = deque()
        self.model = None
        self.model_nom = "baseline-statistique"
        try:
            with open(model_path, "rb") as f:
                self.model = pickle.load(f)
            self.model_nom = "pkl:" + os.path.basename(model_path)
            log.info("Modèle IA environnement chargé : %s", model_path)
        except FileNotFoundError:
            log.warning("Modèle IA environnement absent (%s) : baseline statistique utilisée.", model_path)
        except Exception as e:  # pickle corrompu, version sklearn différente...
            log.error("Chargement du modèle IA impossible (%s) : baseline utilisée.", e)

    # ---- fenêtre ---------------------------------------------------------------------
    def _alimenter(self, c: Capteurs, now: float):
        self.fenetre.append((now, c.temperature, c.humidite, c.gaz))
        while self.fenetre and now - self.fenetre[0][0] > FENETRE_S:
            self.fenetre.popleft()

    def _serie(self, idx):
        pts = [(p[0], p[idx]) for p in self.fenetre if p[idx] is not None]
        return [p[0] for p in pts], [p[1] for p in pts]

    # ---- modèle externe ----------------------------------------------------------------
    def _predire_modele(self, x):
        X = np.asarray([x], dtype=float)
        try:
            y = self.model.predict(X)
        except AttributeError:          # modèle Orange appelé comme une fonction
            y = self.model(X)
        return int(np.asarray(y).ravel()[0])

    # ---- API ---------------------------------------------------------------------------
    def analyser(self, c: Capteurs, now: Optional[float] = None) -> EnvResult:
        now = c.ts if now is None else now
        self._alimenter(c, now)
        ts_t, v_t = self._serie(1)
        ts_g, v_g = self._serie(3)
        pente_t, z_t = _pente_z(ts_t, v_t, PLANCHER_SIGMA_TEMP)
        pente_g, z_g = _pente_z(ts_g, v_g, PLANCHER_SIGMA_GAZ)
        categorie = _categorie(c.temperature, c.gaz, pente_t, pente_g)

        niveau, score, raison, modele = 0, 0.0, "", self.model_nom

        # 1) IA (modèle .pkl s'il existe, sinon baseline statistique)
        modele_ok = False
        if self.model is not None and None not in (c.temperature, c.humidite, c.gaz):
            try:
                y = self._predire_modele([c.temperature, c.humidite, c.gaz, pente_t, pente_g])
                if MODEL_TYPE == "anomalie":
                    niveau = 1 if y == -1 else 0
                else:
                    niveau = max(0, min(2, y))
                score = niveau / 2.0
                raison = f"modèle IA : niveau {niveau}"
                modele_ok = True
            except Exception as e:
                log.error("Prédiction IA en échec (%s) -> baseline.", e)
                self.model = None
                self.model_nom = "baseline-statistique"
                modele = self.model_nom
        if not modele_ok:
            derive_gaz = pente_g >= PENTE_GAZ_ANOMALIE and z_g >= Z_FORT
            derive_temp = pente_t >= PENTE_TEMP_ANOMALIE and z_t >= Z_FORT
            correl = (pente_t >= PENTE_TEMP_CORREL and pente_g >= PENTE_GAZ_CORREL
                      and z_t >= Z_FAIBLE and z_g >= Z_FAIBLE)
            if derive_gaz and derive_temp:
                niveau, score = 2, min(1.0, 0.5 + z_g / 20 + z_t / 20)
                raison = f"hausse simultanée gaz (+{pente_g:.0f}/min) et température (+{pente_t:.1f}°C/min)"
            elif derive_gaz:
                niveau, score = 1, min(1.0, 0.3 + z_g / 20)
                raison = f"dérive du gaz (+{pente_g:.0f}/min, signif.={z_g:.1f})"
            elif derive_temp:
                niveau, score = 1, min(1.0, 0.3 + z_t / 20)
                raison = f"dérive de température (+{pente_t:.1f}°C/min, signif.={z_t:.1f})"
            elif correl:
                niveau, score = 1, min(1.0, 0.25 + (z_t + z_g) / 30)
                raison = f"corrélation température/gaz suspecte (+{pente_t:.1f}°C/min, +{pente_g:.0f}/min)"

        # 2) Filet de sécurité : limites absolues, toujours appliquées
        lim, lim_cat, lim_raison = 0, None, ""
        if c.gaz is not None and c.gaz >= GAZ_CRITIQUE:
            lim, lim_raison = 2, f"gaz {c.gaz:.0f} >= {GAZ_CRITIQUE}"
        elif c.temperature is not None and c.temperature >= TEMP_CRITIQUE:
            lim, lim_raison = 2, f"température {c.temperature:.1f}°C >= {TEMP_CRITIQUE:.0f}"
        elif c.gaz is not None and c.gaz >= GAZ_PREALERTE:
            lim, lim_raison = 1, f"gaz {c.gaz:.0f} >= {GAZ_PREALERTE}"
        elif c.temperature is not None and c.temperature >= TEMP_PREALERTE:
            lim, lim_raison = 1, f"température {c.temperature:.1f}°C >= {TEMP_PREALERTE:.0f}"
        elif c.humidite is not None and (c.humidite <= HUM_BASSE or c.humidite >= HUM_HAUTE):
            lim, lim_cat, lim_raison = 1, "humidite", f"humidité {c.humidite:.0f}%"
        if lim > niveau:
            niveau, score = lim, max(score, 0.6 if lim == 1 else 0.95)
            raison = (raison + " + " if raison else "") + "limite de sécurité : " + lim_raison
            modele = modele + "+limites"
            if lim_cat:
                categorie = lim_cat

        if niveau == 0:
            return EnvResult(0, 0.0, "aucune", "", modele)
        return EnvResult(niveau, round(float(score), 3), categorie, raison, modele)
