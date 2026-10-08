"""
Tests de l'IA analytique (moyennes par minute sur 5 min + seuils), sans base ni matériel.
Lancer depuis le dossier backend :   python -m unittest discover -s tests -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics import analyser_serie, extraire_features, FEATURES, AnalyticsAI, CONFIG  # noqa: E402


def serie(temp=lambda m: 22.0, hum=lambda m: 48.0, gaz=lambda m: 150.0, minutes=5, pas=2):
    """Une mesure toutes les `pas` secondes pendant `minutes` minutes ; les fonctions reçoivent la minute (0 = la plus ancienne)."""
    lignes = []
    for s in range(0, minutes * 60, pas):
        m = s // 60
        lignes.append(dict(ts=1000.0 + s, temperature=temp(m), humidite=hum(m), gaz=gaz(m)))
    return lignes


class TestAnalytics(unittest.TestCase):
    def test_normal(self):
        r = analyser_serie(serie())
        self.assertEqual(r["niveau"], 0)
        self.assertEqual(r["categorie"], "aucune")
        self.assertEqual(len(r["metriques"]["gaz"]["moyennes_minute"]), 5)

    def test_humidite_variation_reguliere_5pct_normale(self):
        r = analyser_serie(serie(hum=lambda m: 40.0 * 1.05 ** m))      # +5 %/min en permanence, reste < 70 %
        self.assertEqual(r["niveau"], 0)

    def test_humidite_saut_10pct_critique(self):
        r = analyser_serie(serie(hum=lambda m: 45.0 if m < 4 else 52.0))   # +15 % sur une minute
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["categorie"], "humidite")

    def test_humidite_limites(self):
        c = CONFIG["humidite"]
        milieu = (c["alerte_min"] + c["alerte_max"]) / 2
        self.assertEqual(analyser_serie(serie(hum=lambda m: c["max"]))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(hum=lambda m: (c["alerte_max"] + c["max"]) / 2))["niveau"], 1)   # pré-alerte
        self.assertEqual(analyser_serie(serie(hum=lambda m: milieu))["niveau"], 0)
        self.assertEqual(analyser_serie(serie(hum=lambda m: c["min"]))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(hum=lambda m: (c["min"] + c["alerte_min"]) / 2))["niveau"], 1)

    def test_temperature_limites(self):
        c = CONFIG["temperature"]
        self.assertEqual(analyser_serie(serie(temp=lambda m: c["max"]))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(temp=lambda m: (c["alerte_max"] + c["max"]) / 2))["niveau"], 1)
        self.assertEqual(analyser_serie(serie(temp=lambda m: 22.0))["niveau"], 0)
        self.assertEqual(analyser_serie(serie(temp=lambda m: c["min"]))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(temp=lambda m: c["max"] + 5))["categorie"], "surchauffe")

    def test_temperature_variation_max(self):
        vm = CONFIG["temperature"]["var_max"]
        r = analyser_serie(serie(temp=lambda m: 20.0 + 0.5 * vm * (m % 2)))        # oscille de la moitié du max : normal
        self.assertEqual(r["niveau"], 0)
        r = analyser_serie(serie(temp=lambda m: 20.0 if m < 4 else 20.0 + 1.5 * vm))   # saut de 1,5 x le max sur une minute
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["categorie"], "derive")
        self.assertIn("temperature", r["raison"])

    def test_gaz_variation_et_seuils_provisoires(self):
        r = analyser_serie(serie(gaz=lambda m: 150.0 if m < 4 else 175.0))   # +16 %
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["categorie"], "gaz")
        self.assertEqual(analyser_serie(serie(gaz=lambda m: 650.0))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(gaz=lambda m: 450.0))["niveau"], 1)

    def test_incendie_temp_et_gaz(self):
        r = analyser_serie(serie(temp=lambda m: CONFIG['temperature']['max'] + 5, gaz=lambda m: 450.0))
        self.assertEqual(r["categorie"], "incendie")

    def test_donnees_insuffisantes(self):
        self.assertEqual(analyser_serie([])["niveau"], 0)
        self.assertEqual(analyser_serie(serie(minutes=1, pas=30)[:1])["niveau"], 0)


def rejouer(generateur, ticks, modele=None):
    """Rejoue un scénario capteurs (1 tick = 2 s) comme le backend : à chaque mesure, analyse des 5 dernières minutes.

    Renvoie la liste des (tick, résultat, gaz, température)."""
    lignes, sorties = [], []
    for t in range(ticks):
        temp, hum, gaz = generateur(t)
        lignes.append(dict(ts=1000.0 + 2 * t, temperature=temp, humidite=hum, gaz=gaz))
        lignes = [l for l in lignes if lignes[-1]["ts"] - l["ts"] <= 300]
        sorties.append((t, analyser_serie(lignes, modele), gaz, temp))
    return sorties


class TestDerivesEtSecurite(unittest.TestCase):
    """Dérives lentes (pente + significativité) et limites de sécurité, reprises de l'ancien env_ai."""

    def setUp(self):
        random.seed(1)

    def test_pas_de_fausse_alerte_en_regime_normal(self):
        g = lambda t: (22 + random.uniform(-.2, .2), 48 + random.uniform(-.5, .5), random.randint(140, 160))
        self.assertTrue(all(r["niveau"] == 0 for _, r, _, _ in rejouer(g, 200)))

    def test_fuite_de_gaz_detectee_avant_le_seuil_dur(self):
        g = lambda t: (22.0, 48.0, 150 + max(0, t - 15) * 14)
        res = rejouer(g, 60)
        premier = next(i for i, (t, r, _, _) in enumerate(res) if r["niveau"] >= 1)
        self.assertLess(res[premier][2], 400)                          # détectée avant la pré-alerte à 400
        self.assertEqual(res[premier][1]["categorie"], "gaz")
        self.assertTrue(any(r["niveau"] == 2 for _, r, _, _ in res))   # puis critique >= 600

    def test_incendie_temperature_et_gaz(self):
        g = lambda t: (22 + max(0, t - 15) * 0.25, 48.0, 150 + max(0, t - 15) * 12)
        res = rejouer(g, 60)
        self.assertTrue(any(r["niveau"] == 2 and r["categorie"] == "incendie" for _, r, _, _ in res))

    def test_derive_lente_correlee(self):
        g = lambda t: (22 + max(0, t - 15) * 0.05, 48.0, 150 + max(0, t - 15) * 0.9)
        res = rejouer(g, 90)
        premier = next((i for i, (t, r, _, _) in enumerate(res) if r["niveau"] >= 1), None)
        self.assertIsNotNone(premier, "la dérive lente doit être détectée")
        self.assertLess(res[premier][2], 400)

    def test_limites_de_securite_independantes_de_config(self):
        ancien = {k: dict(v) for k, v in CONFIG.items()}
        try:
            for v in CONFIG.values():                                  # CONFIG réglé n'importe comment (trop large)
                v.update(min=None, max=None, alerte_min=None, alerte_max=None, var_max=None)
            r = analyser_serie(serie(gaz=lambda m: 700.0))
            self.assertEqual((r["niveau"], r["categorie"]), (2, "gaz"))
            r = analyser_serie(serie(temp=lambda m: 55.0))
            self.assertEqual((r["niveau"], r["categorie"]), (2, "surchauffe"))
            self.assertEqual(analyser_serie(serie(hum=lambda m: 96.0))["niveau"], 1)
        finally:
            for k, v in ancien.items():
                CONFIG[k].clear()
                CONFIG[k].update(v)

    def test_valeurs_manquantes_dht_en_erreur(self):
        lignes = [dict(ts=1000.0 + 2 * i, temperature=None, humidite=None, gaz=150.0) for i in range(100)]
        self.assertEqual(analyser_serie(lignes)["niveau"], 0)

    def test_secours_memoire_si_base_indisponible(self):
        ia = AnalyticsAI(model_path="/inexistant.pkcls")
        ia._tampon.extend(dict(ts=1000.0 + 2 * i, temperature=22.0, humidite=48.0, gaz=150.0 + 14 * i) for i in range(60))
        import sys as _s, types as _t                                  # base simulée en panne
        faux = _t.ModuleType("db")
        faux.get_recent_series = lambda secondes: (_ for _ in ()).throw(RuntimeError("base HS"))
        ancien = _s.modules.get("db")
        _s.modules["db"] = faux
        try:
            r = ia.analyser(None, 1000.0 + 120)
        finally:
            if ancien is None:
                del _s.modules["db"]
            else:
                _s.modules["db"] = ancien
        self.assertGreaterEqual(r.niveau, 1)
        self.assertEqual(r.categorie, "gaz")


class ModeleBidon:
    """Modèle scikit-learn factice : renvoie toujours la même classe."""
    def __init__(self, classe):
        self.classe = classe

    def predict(self, X):
        assert len(X[0]) == len(FEATURES)
        return [self.classe]


class ModeleCasse:
    def predict(self, X):
        raise RuntimeError("boom")


class TestModeleVariations(unittest.TestCase):
    def test_features(self):
        f = extraire_features(serie(hum=lambda m: 45.0 if m < 3 else 52.0))
        self.assertEqual(set(f), set(FEATURES))
        self.assertGreater(f["humidite_var_max"], 10)
        self.assertAlmostEqual(f["gaz_var_max"], 0.0, places=6)

    def test_le_modele_decide_des_variations(self):
        r = analyser_serie(serie(hum=lambda m: 45.0 if m < 4 else 52.0), ModeleBidon(2))   # variation en cours + le modèle dit "critique"
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["methode_variation"], "ia")
        r = analyser_serie(serie(hum=lambda m: 45.0 if m < 4 else 52.0), ModeleBidon(0))
        self.assertEqual(r["niveau"], 0)                            # l'IA l'emporte sur la règle simple

    def test_retour_a_la_normale_apres_un_pic(self):
        pic = lambda m: 45.0 if m != 1 else 60.0                    # pic il y a 3 minutes, tout est rentré dans l'ordre
        self.assertEqual(analyser_serie(serie(hum=pic))["niveau"], 0)                 # règle simple
        self.assertEqual(analyser_serie(serie(hum=pic), ModeleBidon(2))["niveau"], 0)  # IA : plus de variation en cours

    def test_limites_absolues_restent_actives_avec_le_modele(self):
        self.assertEqual(analyser_serie(serie(temp=lambda m: CONFIG['temperature']['max'] + 5), ModeleBidon(0))["niveau"], 2)

    def test_modele_en_panne_retombe_sur_la_regle(self):
        r = analyser_serie(serie(hum=lambda m: 45.0 if m < 4 else 52.0), ModeleCasse())
        self.assertEqual(r["methode_variation"], "regle")
        self.assertEqual(r["niveau"], 2)

    def test_fichier_modele_absent(self):
        self.assertIsNone(AnalyticsAI(model_path="/inexistant.pkcls").modele)


if __name__ == "__main__":
    unittest.main()
