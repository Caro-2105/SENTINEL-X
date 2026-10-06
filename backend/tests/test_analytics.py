"""
Tests de l'IA analytique (moyennes par minute sur 5 min + seuils), sans base ni matériel.
Lancer depuis le dossier backend :   python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analytics import analyser_serie, fusionner  # noqa: E402
from scenarios import EnvResult  # noqa: E402


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
        r = analyser_serie(serie(hum=lambda m: 45.0 if m < 3 else 52.0))   # +15 % sur une minute
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["categorie"], "humidite")

    def test_humidite_limites(self):
        self.assertEqual(analyser_serie(serie(hum=lambda m: 75.0))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(hum=lambda m: 72.0))["niveau"], 1)    # pré-alerte
        self.assertEqual(analyser_serie(serie(hum=lambda m: 69.0))["niveau"], 0)
        self.assertEqual(analyser_serie(serie(hum=lambda m: 30.0))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(hum=lambda m: 33.0))["niveau"], 1)

    def test_temperature_limites_15_25(self):
        self.assertEqual(analyser_serie(serie(temp=lambda m: 25.0))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(temp=lambda m: 24.5))["niveau"], 1)
        self.assertEqual(analyser_serie(serie(temp=lambda m: 23.0))["niveau"], 0)
        self.assertEqual(analyser_serie(serie(temp=lambda m: 15.0))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(temp=lambda m: 16.0))["niveau"], 1)
        self.assertEqual(analyser_serie(serie(temp=lambda m: 30.0))["categorie"], "surchauffe")

    def test_temperature_variation_2_degres(self):
        r = analyser_serie(serie(temp=lambda m: 20.0 + 1.5 * m))       # +1,5 °C/min régulier (jusqu'à 26 : on coupe avant)
        self.assertEqual(analyser_serie(serie(temp=lambda m: 20.0 + 1.5 * (m % 2)))["niveau"], 0)
        r = analyser_serie(serie(temp=lambda m: 20.0 if m < 3 else 22.5))   # +2,5 °C sur une minute, reste < 24
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["categorie"], "derive")
        self.assertIn("temperature", r["raison"])

    def test_gaz_variation_et_seuils_provisoires(self):
        r = analyser_serie(serie(gaz=lambda m: 150.0 if m < 3 else 175.0))   # +16 %
        self.assertEqual(r["niveau"], 2)
        self.assertEqual(r["categorie"], "gaz")
        self.assertEqual(analyser_serie(serie(gaz=lambda m: 650.0))["niveau"], 2)
        self.assertEqual(analyser_serie(serie(gaz=lambda m: 450.0))["niveau"], 1)

    def test_incendie_temp_et_gaz(self):
        r = analyser_serie(serie(temp=lambda m: 30.0, gaz=lambda m: 450.0))
        self.assertEqual(r["categorie"], "incendie")

    def test_donnees_insuffisantes(self):
        self.assertEqual(analyser_serie([])["niveau"], 0)
        self.assertEqual(analyser_serie(serie(minutes=1, pas=30)[:1])["niveau"], 0)

    def test_fusion_garde_le_pire(self):
        a, b = EnvResult(1, 0.5, "gaz", "x", "m1"), EnvResult(2, 0.9, "incendie", "y", "m2")
        self.assertEqual(fusionner(a, b).niveau, 2)
        self.assertEqual(fusionner(EnvResult(0, 0.0, "aucune", "", "m"), None).niveau, 0)
        self.assertEqual(fusionner(None, a), a)


if __name__ == "__main__":
    unittest.main()
