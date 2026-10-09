"""Tests de meteo_risque.py : données synthétiques, aucun accès réseau."""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import meteo_risque as m  # noqa: E402


def serie_synthetique(heures, debut=datetime(2022, 1, 1), graine=0, tempetes=True):
    """Série horaire plausible : la pression chute AVANT chaque tempête (rafales fortes + pluie)."""
    rng = np.random.default_rng(graine)
    temps = [(debut + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(heures)]
    h = np.arange(heures)
    T = 12 + 8 * np.sin(2 * np.pi * (h / 24 / 365.25 - 0.28)) + 3 * np.sin(2 * np.pi * h / 24) + rng.normal(0, 0.5, heures)
    P = 1015 + np.cumsum(rng.normal(0, 0.15, heures)) * 0.2
    P = 1015 + 0.9 * (P - 1015)
    G = np.abs(rng.normal(22, 7, heures))
    R = np.where(rng.random(heures) < 0.08, rng.exponential(0.8, heures), 0.0)
    if tempetes:
        for debut_t in rng.choice(np.arange(48, heures - 48), size=max(1, heures // 700), replace=False):
            P[debut_t - 14:debut_t] -= np.linspace(0, 25, 14)            # chute de pression nette
            P[debut_t:debut_t + 10] = P[debut_t - 1]
            G[debut_t + 2:debut_t + 9] = rng.uniform(65, 100, 7)
            R[debut_t + 2:debut_t + 9] = rng.uniform(4, 9, 7)
    return {"time": temps, "temperature_2m": T, "relative_humidity_2m": np.clip(70 + rng.normal(0, 10, heures), 20, 100),
            "precipitation": R, "pressure_msl": P, "cloud_cover": rng.uniform(0, 100, heures),
            "wind_speed_10m": G * 0.5, "wind_gusts_10m": G}


class TestRegles(unittest.TestCase):
    def test_niveaux(self):
        S = serie_synthetique(100, tempetes=False)
        S["wind_gusts_10m"][:] = 10.0
        S["wind_gusts_10m"][50] = 70.0     # niveau 1
        S["wind_gusts_10m"][60] = 95.0     # niveau 2
        S["temperature_2m"][:] = 15.0
        S["precipitation"][:] = 0.0
        niv, _ = m.niveaux_par_danger(S)
        self.assertEqual((niv["vent"][50], niv["vent"][60], niv["vent"][10]), (1, 2, 0))
        self.assertEqual(int(niv["chaleur"].max()), 0)

    def test_froid_et_chaleur(self):
        S = serie_synthetique(100, tempetes=False)
        S["temperature_2m"][:] = 15.0
        S["temperature_2m"][10] = -6.0
        S["temperature_2m"][20] = 41.0
        niv, _ = m.niveaux_par_danger(S)
        self.assertEqual((int(niv["froid"][10]), int(niv["chaleur"][20])), (1, 2))

    def test_cumul_pluie_6h(self):
        S = serie_synthetique(100, tempetes=False)
        S["precipitation"][:] = 0.0
        S["precipitation"][40:46] = 4.0    # 24 mm sur 6 h
        niv, val = m.niveaux_par_danger(S)
        self.assertEqual(int(niv["pluie"][45]), 1)
        self.assertAlmostEqual(float(val["pluie"][45]), 24.0)
        self.assertEqual(int(niv["pluie"][52]), 0)

    def test_vigilance_ordre_et_quand(self):
        S = serie_synthetique(200, tempetes=False)
        S["wind_gusts_10m"][:] = 10.0
        S["temperature_2m"][:] = 15.0
        S["precipitation"][:] = 0.0
        S["wind_gusts_10m"][120] = 100.0
        d = m.vigilance(S, 100)
        self.assertEqual(len(d), 1)
        self.assertEqual((d[0]["type"], d[0]["niveau"], d[0]["valeur"]), ("vent", 2, 100.0))
        self.assertTrue(d[0]["quand"].endswith("Z"))
        self.assertEqual(m.vigilance(S, 130), [])        # l'événement est passé

    def test_hors_horizon_ignore(self):
        S = serie_synthetique(400, tempetes=False)
        S["wind_gusts_10m"][:] = 10.0
        S["temperature_2m"][:] = 15.0
        S["precipitation"][:] = 0.0
        S["wind_gusts_10m"][300] = 100.0
        self.assertEqual(m.vigilance(S, 100), [])


class TestCaracteristiques(unittest.TestCase):
    def test_forme_et_tendance(self):
        S = serie_synthetique(300)
        X = m.caracteristiques(S)
        self.assertEqual(X.shape, (300, len(m.NOMS_CARACTERISTIQUES)))
        self.assertTrue(np.isfinite(X).all())
        i = m.NOMS_CARACTERISTIQUES.index("chute_pression_3h")
        self.assertAlmostEqual(X[100, i], S["pressure_msl"][100] - S["pressure_msl"][97])

    def test_etiquettes_regardent_le_futur(self):
        S = serie_synthetique(200, tempetes=False)
        S["wind_gusts_10m"][:] = 10.0
        S["temperature_2m"][:] = 15.0
        S["precipitation"][:] = 0.0
        S["wind_gusts_10m"][100] = 80.0
        y = m.etiquettes(S)
        self.assertEqual(len(y), 200 - m.HORIZON_IA_H)
        self.assertEqual((y[99], y[76], y[75], y[100], y[101]), (1, 1, 0, 0, 0))   # t=100 exclu, 24 h avant inclus

    def test_combler(self):
        a = np.array([np.nan, 1.0, np.nan, np.nan, 4.0])
        self.assertEqual(list(m._combler(a)), [1.0, 1.0, 1.0, 1.0, 4.0])
        self.assertEqual(list(m._combler(a, defaut=0.0)), [0.0, 1.0, 0.0, 0.0, 4.0])


class TestIA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import sklearn  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("scikit-learn absent")
        cls.S = serie_synthetique(24 * 365 * 3, graine=1)
        cls.res = m.entrainer(cls.S)

    def test_apprend_mieux_que_le_hasard(self):
        self.assertIsNotNone(self.res["auc"])
        self.assertGreater(self.res["auc"], 0.75)

    def test_proba_plus_haute_avant_la_tempete(self):
        S2 = serie_synthetique(24 * 120, graine=7)
        p = self.res["modele"].predict_proba(m.caracteristiques(S2))[:, 1]
        y = m.etiquettes(S2)
        self.assertGreater(p[:len(y)][y == 1].mean(), 2 * p[:len(y)][y == 0].mean())

    def test_trop_peu_d_evenements(self):
        calme = serie_synthetique(24 * 200, tempetes=False)
        calme["wind_gusts_10m"][:] = 10.0
        with self.assertRaises(m.MeteoErreur):
            m.entrainer(calme)

    def test_niveau_ia(self):
        self.assertEqual((m.niveau_ia(0.05, 0.02), m.niveau_ia(0.35, 0.02), m.niveau_ia(0.8, 0.02)), (0, 1, 2))
        self.assertEqual(m.niveau_ia(0.35, 0.2), 0)       # base rate élevée : 0.35 n'est pas remarquable


class TestService(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.mkdtemp()
        self.now = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc).timestamp()
        debut = datetime(2026, 10, 5, 0, 0)       # past_days=3 puis 4 jours de prévision
        self.S = serie_synthetique(24 * 7, debut=debut, tempetes=False)
        self.S["wind_gusts_10m"][:] = 12.0
        self.S["temperature_2m"][:] = 14.0
        self.S["precipitation"][:] = 0.0
        self.S["wind_gusts_10m"][24 * 3 + 12 + 20] = 95.0     # dans 20 h
        self.appels = []

    def service(self, arch=None):
        def prev(lat, lon):
            self.appels.append((lat, lon))
            return self.S

        def arch_ko(*a):
            raise m.MeteoErreur("pas d'Internet")

        geo = lambda nom: {"nom": nom, "lat": 45.76, "lon": 4.84, "pays": "France"}
        return m.ServiceMeteo(self.dossier, prev, arch or arch_ko, geo, horloge=lambda: self.now)

    def test_calcul_sans_ia(self):
        s = self.service()
        r = s.calculer()
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["dangers"][0]["type"], "vent")
        self.assertEqual(r["serie"][0]["t"], "2026-10-08T12:00Z")
        self.assertEqual(len(r["serie"]), m.HORIZON_PREVISION_H + 1)
        self.assertIn(r["ia"]["etat"], ("entrainement", "indisponible"))
        self.assertIsNone(r["serie"][0]["proba"])

    def test_alarme_si_danger_proche(self):
        s = self.service()
        r = s.calculer()                        # rafales 95 km/h dans 20 h : dans la fenêtre d'alarme (24 h)
        self.assertEqual((r["alarme"]["niveau"], r["alarme"]["categorie"]), (2, "meteo_vent"))
        s._resultat = r
        self.assertEqual(s.alarme()["categorie"], "meteo_vent")

    def test_pas_d_alarme_si_danger_lointain(self):
        self.S["wind_gusts_10m"][:] = 12.0
        self.S["wind_gusts_10m"][24 * 3 + 12 + 50] = 95.0     # dans 50 h : affiché mais pas d'alarme
        s = self.service()
        r = s.calculer()
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["alarme"]["niveau"], 0)
        s._resultat = r
        self.assertIsNone(s.alarme())

    def test_alarme_perimee_ignoree(self):
        s = self.service()
        s._resultat = s.calculer()
        self.now += m.ALARME_PEREMPTION_S + 60
        self.assertIsNone(s.alarme())

    def test_donnees_ne_bloque_pas_et_met_en_cache(self):
        s = self.service()
        self.assertEqual(s.donnees()["etat"], "chargement")
        for _ in range(100):
            time.sleep(0.05)
            if s.donnees()["etat"] == "ok":
                break
        self.assertEqual(s.donnees()["etat"], "ok")
        n = len(self.appels)
        s.donnees()
        s.donnees()
        time.sleep(0.2)
        self.assertEqual(len(self.appels), n)              # cache de 15 min : pas de nouvel appel

    def test_erreur_reseau_remontee(self):
        def prev(*a):
            raise m.MeteoErreur("hors ligne")
        s = m.ServiceMeteo(self.dossier, prev, lambda *a: None, lambda n: {}, horloge=lambda: self.now)
        s.donnees()
        time.sleep(0.3)
        d = s.donnees()
        self.assertEqual((d["etat"], d["message"]), ("erreur", "hors ligne"))

    def test_changer_lieu_persiste(self):
        s = self.service()
        lieu = s.changer_lieu("Lyon")
        self.assertEqual(lieu["nom"], "Lyon")
        self.assertEqual(self.service().lieu["nom"], "Lyon")     # relu depuis meteo_lieu.json
        with self.assertRaises(m.MeteoErreur):
            s.changer_lieu("   ")

    def test_entrainement_puis_ia(self):
        try:
            import sklearn  # noqa: F401
        except ImportError:
            self.skipTest("scikit-learn absent")
        hist = serie_synthetique(24 * 365 * 3, graine=1)
        s = self.service(arch=lambda *a: hist)
        s.calculer()                                        # lance l'entraînement en fond
        for _ in range(300):
            time.sleep(0.1)
            if s._modele is not None:
                break
        self.assertIsNotNone(s._modele)
        r = s.calculer()
        self.assertEqual(r["ia"]["etat"], "pret")
        self.assertIsNotNone(r["serie"][0]["proba"])
        self.assertTrue(any(f.endswith(".pkl") for f in os.listdir(self.dossier)))
        # un nouveau service recharge le modèle sauvegardé sans réentraîner
        s2 = self.service()
        self.assertEqual(s2.calculer()["ia"]["etat"], "pret")


if __name__ == "__main__":
    unittest.main()
