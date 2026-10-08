import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ordres_esp import OrdresEsp, mise_en_page  # noqa: E402


def sujets(envois):
    return [t.rsplit("/", 1)[-1] for t, _ in envois]


class TestOrdresEsp(unittest.TestCase):
    def test_mise_en_page_deux_lignes(self):
        self.assertEqual(mise_en_page("INTRUSION", "Personne inconnue"), "INTRUSION       Personne inconnue"[:32])
        self.assertEqual(len(mise_en_page("A", "B")), 17)
        self.assertEqual(mise_en_page("SENTINEL-X", "Surveillance OK"), "")        # veille : écran vide

    def test_prenom_long_et_accents(self):
        self.assertEqual(mise_en_page("ACCES AUTORISE", "Bienvenue Caroline")[16:], "Caroline")
        self.assertEqual(mise_en_page("ALERTE", 'Fumée "ok"')[16:], "Fumee 'ok'")

    def test_acces_autorise_ecran_et_melodie_une_seule_fois(self):
        o = OrdresEsp()
        self.assertEqual(sujets(o.traiter(0, "ACCES AUTORISE", "Bienvenue Killian", 0)), ["screen", "granted"])
        self.assertEqual(o.traiter(0, "ACCES AUTORISE", "Bienvenue Killian", 2), [])      # heartbeat du back : rien

    def test_intrusion_repete_le_son_sans_reecrire_l_ecran(self):
        o = OrdresEsp()
        self.assertEqual(sujets(o.traiter(2, "INTRUSION", "Personne inconnue", 0)), ["screen", "denied"])
        self.assertEqual(o.traiter(2, "INTRUSION", "Personne inconnue", 2), [])           # trop tôt (< 3 s)
        self.assertEqual(sujets(o.traiter(2, "INTRUSION", "Personne inconnue", 4)), ["denied"])

    def test_anomalie_son_toutes_les_10_s(self):
        o = OrdresEsp()
        self.assertEqual(sujets(o.traiter(1, "ATTENTION", "Surchauffe", 0)), ["screen", "denied"])
        self.assertEqual(o.traiter(1, "ATTENTION", "Surchauffe", 8), [])
        self.assertEqual(sujets(o.traiter(1, "ATTENTION", "Surchauffe", 10)), ["denied"])

    def test_identification_sans_son_de_melodie(self):
        o = OrdresEsp()
        self.assertEqual(sujets(o.traiter(0, "IDENTIFICATION", "Regardez la camera", 0)), ["screen"])

    def test_retour_au_calme_efface_l_ecran(self):
        o = OrdresEsp()
        o.traiter(2, "ALERTE DANGER", "Fuite de gaz", 0)
        envois = o.traiter(0, "SENTINEL-X", "Surveillance OK", 2)
        self.assertEqual(sujets(envois), ["screen"])
        self.assertEqual(envois[0][1], '{"str":""}')


if __name__ == "__main__":
    unittest.main()
