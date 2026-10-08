"""
Tests de l'authentification (visage puis mot de passe), sans matériel ni base de données.
Lancer depuis le dossier backend :   python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auth  # noqa: E402
from auth import Authentificateur, AuthError  # noqa: E402

auth.FACE_CONSECUTIVES = 3        # les tests ci-dessous vérifient la logique « N identifications successives »
FACE_CONSECUTIVES = 3

MEMBRES = {"Caroline": {"id": 1, "nom": "Caroline", "actif": True},
           "Killian": {"id": 2, "nom": "Killian", "actif": False}}
MDP = "Sentinel2026x"
HASH = auth.hash_password(MDP)
HASHES = {"Caroline": HASH}


def hash_de(u):
    return HASHES.get(u)


class TestMotsDePasse(unittest.TestCase):
    def test_hash_et_verification(self):
        self.assertTrue(auth.verify_password(MDP, HASH))
        self.assertFalse(auth.verify_password("autre", HASH))
        self.assertFalse(auth.verify_password(MDP, "n'importe quoi"))
        self.assertNotIn(MDP, HASH)                       # jamais en clair
        self.assertNotEqual(auth.hash_password(MDP), HASH)   # sel aléatoire

    def test_force(self):
        self.assertIsNotNone(auth.verifier_force("court1A"))
        self.assertIsNotNone(auth.verifier_force("toutenminuscule1"))
        self.assertIsNotNone(auth.verifier_force("SansChiffreIci"))
        self.assertIsNone(auth.verifier_force(MDP))


class Base(unittest.TestCase):
    def setUp(self):
        self.a = Authentificateur()
        self.t = 1000.0

    def vision(self, label="Caroline", conf=0.95, connu=True, visages=1, fois=1, presence=True):
        res = False
        for _ in range(fois):
            self.t += 1
            res = self.a.sur_vision(label, conf, connu, visages, MEMBRES, self.t, presence) or res
        return res

    def reconnu(self):
        lid = self.a.demarrer(self.t)
        self.assertTrue(self.vision(fois=FACE_CONSECUTIVES))
        return lid


class TestVisage(Base):
    def test_validation_apres_n_identifications(self):
        lid = self.a.demarrer(self.t)
        self.vision(fois=FACE_CONSECUTIVES - 1)
        self.assertEqual(self.a.statut(lid, self.t)["etape"], "visage")
        self.assertTrue(self.vision())
        st = self.a.statut(lid, self.t)
        self.assertEqual((st["etape"], st["utilisateur"]), ("mot_de_passe", "Caroline"))

    def test_une_erreur_remet_le_compteur_a_zero(self):
        lid = self.a.demarrer(self.t)
        self.vision(fois=FACE_CONSECUTIVES - 1)
        self.vision(label="inconnu", connu=False)
        self.vision(fois=FACE_CONSECUTIVES - 1)
        self.assertEqual(self.a.statut(lid, self.t)["etape"], "visage")

    def test_refus(self):
        lid = self.a.demarrer(self.t)
        for kw, msg in [(dict(connu=False, label="inconnu"), "non reconnu"), (dict(visages=0), "Aucun visage"),
                        (dict(visages=2), "Plusieurs"), (dict(conf=0.6), "incertaine"),
                        (dict(label="Killian"), "pas accès"), (dict(label="Fantome"), "pas accès")]:
            self.assertFalse(self.vision(fois=FACE_CONSECUTIVES + 2, **kw), kw)
            self.assertIn(msg, self.a.statut(lid, self.t)["message"])
        self.assertEqual(self.a.statut(lid, self.t)["etape"], "visage")

    def test_alternance_de_deux_visages_ne_valide_pas(self):
        self.a.demarrer(self.t)
        MEMBRES["Florent"] = {"id": 3, "nom": "Florent", "actif": True}
        try:
            for _ in range(6):
                self.assertFalse(self.vision("Caroline"))
                self.assertFalse(self.vision("Florent"))
        finally:
            del MEMBRES["Florent"]

    def test_presence_exigee(self):
        a = Authentificateur(exige_presence=True)
        a.demarrer(self.t)
        for _ in range(FACE_CONSECUTIVES + 2):
            self.t += 1
            self.assertFalse(a.sur_vision("Caroline", .95, True, 1, MEMBRES, self.t, presence_ok=False))
        for _ in range(FACE_CONSECUTIVES):
            self.t += 1
            ok = a.sur_vision("Caroline", .95, True, 1, MEMBRES, self.t, presence_ok=True)
        self.assertTrue(ok)

    def test_expiration_de_la_reconnaissance(self):
        lid = self.a.demarrer(self.t)
        self.assertEqual(self.a.statut(lid, self.t + auth.LOGIN_TTL_S + 1)["etape"], "expire")
        self.assertFalse(self.a.statut("inconnu", self.t)["etape"] == "visage")


class TestMotDePasseEtSession(Base):
    def test_connexion_ok(self):
        lid = self.reconnu()
        jeton, nom = self.a.connexion(lid, MDP, hash_de, self.t)
        self.assertEqual(nom, "Caroline")
        self.assertEqual(self.a.verifier(jeton, self.t)["utilisateur"], "Caroline")

    def test_sans_visage_pas_de_connexion(self):
        lid = self.a.demarrer(self.t)
        with self.assertRaises(AuthError) as c:
            self.a.connexion(lid, MDP, hash_de, self.t)
        self.assertEqual(c.exception.code, "face_requise")
        with self.assertRaises(AuthError):
            self.a.connexion("n'existe pas", MDP, hash_de, self.t)
        # un envoi prématuré ne détruit pas la reconnaissance en cours
        self.assertTrue(self.vision(fois=FACE_CONSECUTIVES))
        self.assertEqual(self.a.statut(lid, self.t)["etape"], "mot_de_passe")

    def test_mauvais_mot_de_passe(self):
        lid = self.reconnu()
        with self.assertRaises(AuthError) as c:
            self.a.connexion(lid, "mauvais", hash_de, self.t)
        self.assertEqual(c.exception.code, "mot_de_passe")
        self.assertIn("4", c.exception.message)

    def test_utilisateur_sans_mot_de_passe_refuse(self):
        lid = self.reconnu()
        with self.assertRaises(AuthError) as c:
            self.a.connexion(lid, MDP, lambda u: None, self.t)
        self.assertEqual(c.exception.code, "sans_mot_de_passe")
        self.assertIn("manage_users.py", c.exception.message)

    def test_message_confiance_insuffisante(self):
        lid = self.a.demarrer(self.t)
        self.vision(conf=0.7)
        m = self.a.statut(lid, self.t)["message"]
        self.assertIn("70 %", m)
        self.assertIn("85 %", m)

    def test_verrouillage_et_recidive(self):
        for _ in range(auth.MAX_ESSAIS):
            lid = self.a.demarrer(self.t) if _ == 0 else lid
            if _ == 0:
                self.vision(fois=FACE_CONSECUTIVES)
            with self.assertRaises(AuthError) as c:
                self.a.connexion(lid, "x" * 10, hash_de, self.t)
        self.assertEqual(c.exception.code, "verrouille")
        self.assertEqual(c.exception.http, 423)
        # même avec le bon mot de passe, pendant le verrouillage
        lid2 = self.reconnu()
        with self.assertRaises(AuthError) as c:
            self.a.connexion(lid2, MDP, hash_de, self.t)
        self.assertEqual(c.exception.code, "verrouille")
        # après le délai : possible à nouveau
        self.t += auth.VERROU_S + 1
        lid3 = self.reconnu()
        jeton, _ = self.a.connexion(lid3, MDP, hash_de, self.t)
        self.assertIsNotNone(self.a.verifier(jeton, self.t))

    def test_reconnaissance_a_usage_unique(self):
        lid = self.reconnu()
        self.a.connexion(lid, MDP, hash_de, self.t)
        with self.assertRaises(AuthError):
            self.a.connexion(lid, MDP, hash_de, self.t)

    def test_mot_de_passe_trop_long_ou_non_texte(self):
        lid = self.reconnu()
        with self.assertRaises(AuthError):
            self.a.connexion(lid, "a" * 5000, hash_de, self.t)
        with self.assertRaises(AuthError):
            self.a.connexion(lid, None, hash_de, self.t)

    def test_delai_pour_saisir_le_mot_de_passe(self):
        lid = self.reconnu()
        with self.assertRaises(AuthError) as c:
            self.a.connexion(lid, MDP, hash_de, self.t + auth.FACE_VALIDE_S + 1)
        self.assertEqual(c.exception.code, "face_requise")

    def test_session_inactivite_et_deconnexion(self):
        jeton, _ = self.a.connexion(self.reconnu(), MDP, hash_de, self.t)
        self.assertIsNotNone(self.a.verifier(jeton, self.t + 600))
        self.assertIsNone(self.a.verifier(jeton, self.t + 600 + auth.SESSION_INACTIVITE_S + 1))
        jeton, _ = self.a.connexion(self.reconnu(), MDP, hash_de, self.t)
        self.a.deconnexion(jeton)
        self.assertIsNone(self.a.verifier(jeton, self.t))
        self.assertIsNone(self.a.verifier("jeton-bidon", self.t))

    def test_session_duree_maximale(self):
        jeton, _ = self.a.connexion(self.reconnu(), MDP, hash_de, self.t)
        debut, t = self.t, self.t
        while t - debut < auth.SESSION_MAX_S:          # activité régulière : l'inactivité ne coupe pas
            t += 600
            self.a.verifier(jeton, t)
        self.assertIsNone(self.a.verifier(jeton, debut + auth.SESSION_MAX_S + 1))


class TestUneSeuleIdentification(unittest.TestCase):
    """Comportement par défaut : une identification >= 85 % d'un membre actif valide l'étape 1."""

    def setUp(self):
        self.ancien, auth.FACE_CONSECUTIVES = auth.FACE_CONSECUTIVES, 1
        self.a = Authentificateur()

    def tearDown(self):
        auth.FACE_CONSECUTIVES = self.ancien

    def test_defaut_est_une_identification(self):
        import importlib
        os.environ.pop("AUTH_IDENTIFICATIONS", None)
        self.assertEqual(importlib.reload(auth).FACE_CONSECUTIVES, 1)
        auth.FACE_CONSECUTIVES = 1

    def test_une_identification_suffit(self):
        lid = self.a.demarrer(1000.0)
        self.assertTrue(self.a.sur_vision("Caroline", 0.86, True, 1, MEMBRES, 1001.0))
        self.assertEqual(self.a.statut(lid, 1001.0)["etape"], "mot_de_passe")

    def test_sous_le_seuil_toujours_refuse(self):
        lid = self.a.demarrer(1000.0)
        for t in range(10):
            self.assertFalse(self.a.sur_vision("Caroline", 0.84, True, 1, MEMBRES, 1001.0 + t))
        self.assertEqual(self.a.statut(lid, 1011.0)["etape"], "visage")

    def test_inconnu_membre_inactif_deux_visages_refuses(self):
        lid = self.a.demarrer(1000.0)
        for args in [("inconnu", .99, False, 1), ("Killian", .99, True, 1), ("Caroline", .99, True, 2),
                     ("Caroline", .99, True, 0)]:
            self.assertFalse(self.a.sur_vision(*args, MEMBRES, 1001.0), args)


class TestLabel(unittest.TestCase):
    def test_valides(self):
        for l in ("Caroline", "Jean-Pierre", "Marie Curie", "O'Neil", "membre_2"):
            self.assertIsNone(auth.verifier_label(l), l)

    def test_invalides(self):
        for l in ("", "   ", " Caroline", "Caroline ", "a" * 41, "x;DROP", "<b>", "dev", "DEV", None):
            self.assertIsNotNone(auth.verifier_label(l), l)


if __name__ == "__main__":
    unittest.main()
