"""
Tests des stories SENTINEL-X, sans matériel ni broker ni base de données.
Lancer depuis le dossier backend :   python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scenarios import (MoteurScenarios, Capteurs, Vision, EnvResult,   # noqa: E402
                       MAINTIEN_ACCES_S, DELAI_IDENTIFICATION_S, VISION_TTL_S)

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


if __name__ == "__main__":
    unittest.main()
