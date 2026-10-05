"""
Tests des stories SENTINEL-X, sans matériel ni broker ni base de données.
Lancer depuis le dossier backend :   python -m unittest discover -s tests -v
"""
import os
import pickle
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scenarios import (MoteurScenarios, Capteurs, Vision, EnvResult,   # noqa: E402
                       MAINTIEN_ACCES_S, DELAI_IDENTIFICATION_S, VISION_TTL_S)
from env_ai import EnvAnalyzer  # noqa: E402

MEMBRES = {"alice": {"id": 1, "nom": "Alice", "actif": True},
           "bob": {"id": 2, "nom": "Bob", "actif": False}}


def cap(now, presence=1, ir=34.5, t=22.0, h=48.0, g=150):
    return Capteurs(temperature=t, humidite=h, gaz=g, presence=presence, ir_temp=ir, ts=now)


def vis(now, label="alice", conf=0.93, connu=True, visages=1):
    return Vision(label=label, confiance=conf, connu=connu, visages=visages, ts=now)


class TestAcces(unittest.TestCase):
    def setUp(self):
        self.m = MoteurScenarios()

    def ev(self, now, c, v, env=None):
        return self.m.evaluer(c, v, env, MEMBRES, now)

    def test_membre_autorise_led_verte_et_message(self):
        d = self.ev(100, cap(100), vis(100))
        self.assertEqual(d.scenarios, ["ACC-01"])
        self.assertTrue(d.led_verte)
        self.assertEqual(d.buzzer, 0)
        self.assertEqual((d.ligne1, d.ligne2), ("ACCES AUTORISE", "Bienvenue Alice"))
        self.assertEqual([e.code for e in d.evenements], ["ACC-01"])

    def test_membre_autorise_sans_capteur_ir(self):
        d = self.ev(100, cap(100, ir=None), vis(100))       # repli : le PIR (infrarouge passif) suffit
        self.assertTrue(d.led_verte)

    def test_visage_seul_sans_presence_n_autorise_pas(self):
        d = self.ev(100, cap(100, presence=0), vis(100))
        self.assertFalse(d.led_verte)
        self.assertEqual(d.scenarios, ["IDLE"])

    def test_inconnu_intrusion(self):
        d = self.ev(100, cap(100), vis(100, "inconnu", 0.4, False))
        self.assertEqual(d.scenarios, ["ACC-02"])
        self.assertFalse(d.led_verte)
        self.assertEqual(d.buzzer, 2)
        self.assertEqual(d.ligne1, "INTRUSION")

    def test_confiance_trop_faible_refusee(self):
        d = self.ev(100, cap(100), vis(100, "alice", 0.5, True))
        self.assertEqual(d.scenarios, ["ACC-02"])

    def test_membre_desactive_refuse(self):
        d = self.ev(100, cap(100), vis(100, "bob", 0.95, True))
        self.assertEqual(d.scenarios, ["ACC-02"])
        self.assertFalse(d.led_verte)

    def test_usurpation_visage_sans_chaleur(self):
        d = self.ev(100, cap(100, ir=21.0), vis(100))
        self.assertEqual(d.scenarios, ["ACC-05"])
        self.assertFalse(d.led_verte)
        self.assertEqual(d.buzzer, 2)

    def test_presence_sans_visage_attente_puis_alerte(self):
        d0 = self.ev(100, cap(100), None)
        self.assertEqual(d0.scenarios, ["ACC-03"])
        self.assertEqual(d0.buzzer, 0)
        d1 = self.ev(100 + DELAI_IDENTIFICATION_S + 1, cap(100 + DELAI_IDENTIFICATION_S + 1), None)
        self.assertEqual(d1.scenarios, ["ACC-04"])
        self.assertEqual(d1.buzzer, 1)

    def test_objet_chaud_sans_visage_ignore(self):
        d = self.ev(100, cap(100, ir=60.0), None)
        self.assertEqual(d.scenarios, ["IDLE"])

    def test_vision_perimee_ignoree(self):
        d = self.ev(100, cap(100), vis(100 - VISION_TTL_S - 1))
        self.assertEqual(d.scenarios, ["ACC-03"])

    def test_maintien_led_verte_puis_extinction(self):
        self.ev(100, cap(100), vis(100))
        d = self.ev(103, cap(103, presence=0), None)          # PIR retombé, LED maintenue
        self.assertTrue(d.led_verte)
        d = self.ev(100 + MAINTIEN_ACCES_S + 1, cap(100 + MAINTIEN_ACCES_S + 1, presence=0), None)
        self.assertFalse(d.led_verte)

    def test_un_seul_evenement_par_apparition(self):
        n = 0
        for i in range(10):
            n += len(self.ev(100 + i, cap(100 + i), vis(100 + i)).evenements)
        self.assertEqual(n, 1)

    def test_chaleur_perimee_attend_la_trame_suivante(self):
        # trame ESP de t=98, visage de t=100 : on n'autorise pas sur une mesure IR plus vieille que le visage
        d = self.ev(100, cap(98), vis(100))
        self.assertEqual(d.scenarios, ["ACC-03"])
        self.assertFalse(d.led_verte)
        self.assertEqual(d.evenements, [])
        d = self.ev(101, cap(101), vis(100))             # trame fraîche reçue : autorisation
        self.assertEqual(d.scenarios, ["ACC-01"])

    def test_capteurs_perimes(self):
        d = self.ev(200, cap(100), vis(200))
        self.assertEqual(d.scenarios, ["IDLE"])


class TestCombinaison(unittest.TestCase):
    def test_membre_present_et_gaz_critique(self):
        m = MoteurScenarios()
        env = EnvResult(2, 0.95, "gaz", "gaz 700", "test")
        d = m.evaluer(cap(100, g=700), vis(100), env, MEMBRES, 100)
        self.assertTrue(d.led_verte)                  # l'accès reste autorisé
        self.assertEqual(d.buzzer, 2)                 # mais l'alarme sonne
        self.assertEqual((d.ligne1, d.ligne2), ("ALERTE DANGER", "Fuite de gaz"))
        self.assertEqual(sorted(e.code for e in d.evenements), ["ACC-01", "ENV-02"])

    def test_env_niveau1(self):
        m = MoteurScenarios()
        d = m.evaluer(cap(100, presence=0), None, EnvResult(1, 0.4, "derive", "", "x"), MEMBRES, 100)
        self.assertEqual((d.buzzer, d.ligne1, d.ligne2), (1, "ATTENTION", "Derive suspecte"))
        self.assertFalse(d.led_verte)


def rejouer(analyseur, generateur, ticks):
    """Rejoue un scénario capteurs (1 tick = 2 s) ; renvoie la liste des (tick, EnvResult, capteurs)."""
    sorties = []
    for t in range(ticks):
        now = 1000 + 2 * t
        temp, hum, gaz = generateur(t)
        c = Capteurs(temperature=temp, humidite=hum, gaz=gaz, presence=0, ts=now)
        sorties.append((t, analyseur.analyser(c, now), c))
    return sorties


class TestEnvAI(unittest.TestCase):
    def setUp(self):
        random.seed(1)
        self.ia = EnvAnalyzer(model_path="/inexistant.pkl")

    def test_pas_de_fausse_alerte_en_regime_normal(self):
        g = lambda t: (22 + random.uniform(-.2, .2), 48 + random.uniform(-.5, .5), random.randint(140, 160))
        res = rejouer(self.ia, g, 200)
        self.assertTrue(all(r.niveau == 0 for _, r, _ in res))

    def test_fuite_de_gaz_detectee_avant_le_seuil_dur(self):
        g = lambda t: (22.0, 48.0, 150 + max(0, t - 15) * 14)
        res = rejouer(self.ia, g, 60)
        premier = next(t for t, r, _ in res if r.niveau >= 1)
        gaz_a_la_detection = res[premier][2].gaz
        self.assertLess(gaz_a_la_detection, 400)          # l'IA devance la pré-alerte à 400
        self.assertEqual(res[premier][1].categorie, "gaz")
        self.assertTrue(any(r.niveau == 2 for _, r, _ in res))   # puis critique >= 600

    def test_incendie_temperature_et_gaz(self):
        g = lambda t: (22 + max(0, t - 15) * 0.25, 48.0, 150 + max(0, t - 15) * 12)
        res = rejouer(self.ia, g, 60)
        self.assertTrue(any(r.niveau == 2 and r.categorie == "incendie" for _, r, _ in res))

    def test_derive_lente_correlee(self):
        g = lambda t: (22 + max(0, t - 15) * 0.05, 48.0, 150 + max(0, t - 15) * 0.9)
        res = rejouer(self.ia, g, 90)
        alertes = [r for _, r, c in res if r.niveau >= 1]
        self.assertTrue(alertes, "la dérive lente doit être détectée")
        self.assertLess(res[next(t for t, r, _ in res if r.niveau >= 1)][2].gaz, 400)

    def test_limite_dure_toujours_active(self):
        r = self.ia.analyser(Capteurs(temperature=22, humidite=48, gaz=700, ts=1000), 1000)
        self.assertEqual((r.niveau, r.categorie), (2, "gaz"))
        r = EnvAnalyzer(model_path="/inexistant.pkl").analyser(
            Capteurs(temperature=55, humidite=48, gaz=150, ts=1000), 1000)
        self.assertEqual((r.niveau, r.categorie), (2, "surchauffe"))

    def test_valeurs_manquantes_dht_en_erreur(self):
        r = self.ia.analyser(Capteurs(temperature=None, humidite=None, gaz=150, ts=1000), 1000)
        self.assertEqual(r.niveau, 0)


class ModeleFactice:
    def __init__(self, sortie):
        self.sortie = sortie

    def predict(self, X):
        return [self.sortie] * len(X)


class TestModelePkl(unittest.TestCase):
    def _charger(self, objet):
        f = tempfile.NamedTemporaryFile(suffix=".pkl", delete=False)
        pickle.dump(objet, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return EnvAnalyzer(model_path=f.name)

    def test_modele_classifieur_utilise(self):
        ia = self._charger(ModeleFactice(2))
        r = ia.analyser(Capteurs(temperature=22, humidite=48, gaz=150, ts=1000), 1000)
        self.assertEqual(r.niveau, 2)
        self.assertTrue(r.modele.startswith("pkl:"))

    def test_modele_defaillant_repli_baseline(self):
        class Casse:
            def predict(self, X):
                raise RuntimeError("boom")
        ia = EnvAnalyzer(model_path="/inexistant.pkl")
        ia.model = Casse()
        r = ia.analyser(Capteurs(temperature=22, humidite=48, gaz=150, ts=1000), 1000)
        self.assertEqual(r.niveau, 0)
        self.assertIsNone(ia.model)


if __name__ == "__main__":
    unittest.main()
