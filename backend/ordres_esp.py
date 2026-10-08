"""
Traduit les ordres du back (buzzer 0/1/2 + 2 lignes d'écran) en messages pour le firmware de l'ESP.

Le firmware (mini-broker uMQTTBroker) ne connaît que des FONCTIONS ponctuelles :

    esp/function/screen    {"str":"<16 car. ligne 1><16 car. ligne 2>"}   efface l'écran, l'écrit, bip court
                           {"str":""} = écran vide, silencieux
    esp/function/granted   mélodie « accès accordé »
    esp/function/denied    mélodie « accès refusé »

Il n'a pas d'alarme continue : le buzzer des niveaux 1 et 2 est donc ÉMULÉ en répétant « denied » tant que la
situation dure (toutes les 3 s pour un danger, toutes les 10 s pour une anomalie). L'écran n'est réécrit qu'à
chaque CHANGEMENT d'état (chaque écriture fait bip + clignote).

Module sans dépendance (ni MQTT ni matériel) : voir tests/test_ordres_esp.py.
"""
import json
import unicodedata

TOPIC_FONCTION = "esp/function"
COLS = 16                                  # écran LCD 16 x 2
PERIODE_SON_S = {1: 10.0, 2: 3.0}          # répétition du son selon le niveau de buzzer du back
LIGNE_VEILLE = "SENTINEL-X"                # texte de repos du back : écran vide, sans bip
ABREGES = {"CONFIANCE TROP FAIBLE": "CONFIANCE FAIBLE"}      # lignes du back de plus de 16 caractères


def ascii_lcd(texte):
    """Le LCD HD44780 n'affiche ni accents ni guillemets (et le firmware coupe le texte au premier guillemet)."""
    t = unicodedata.normalize("NFKD", str(texte)).encode("ascii", "ignore").decode("ascii")
    return t.replace('"', "'").replace("\\", "/")


def mise_en_page(ligne1, ligne2):
    """Texte envoyé à `screen` : ligne 1 complétée à 16 caractères, puis ligne 2 (le firmware coupe à 16)."""
    l1, l2 = ascii_lcd(ligne1), ascii_lcd(ligne2)
    l1 = ABREGES.get(l1.upper(), l1)
    if l1.upper().startswith(LIGNE_VEILLE):
        return ""
    if len(l2) > COLS and l2.startswith("Bienvenue "):
        l2 = l2[len("Bienvenue "):]            # « Bienvenue Caroline » ne tient pas : on garde le prénom
    return l1[:COLS].ljust(COLS) + l2[:COLS]


class OrdresEsp:
    def __init__(self):
        self.cle = None
        self.dernier_son = float("-inf")

    def traiter(self, buzzer, ligne1, ligne2, now):
        """Renvoie la liste [(topic, payload)] à publier vers l'ESP pour cet ordre du back (renvoyé toutes les 2 s)."""
        buzzer = int(buzzer)
        cle = (buzzer, ligne1, ligne2)
        nouveau = cle != self.cle
        envois = []
        if nouveau:
            self.cle = cle
            self.dernier_son = float("-inf")
            envois.append((f"{TOPIC_FONCTION}/screen",
                           json.dumps({"str": mise_en_page(ligne1, ligne2)}, separators=(",", ":"))))
        if buzzer >= 1:
            if now - self.dernier_son >= PERIODE_SON_S.get(buzzer, PERIODE_SON_S[2]):
                self.dernier_son = now
                envois.append((f"{TOPIC_FONCTION}/denied", ""))
        elif nouveau and ascii_lcd(ligne1).upper().startswith("ACCES AUTORISE"):
            envois.append((f"{TOPIC_FONCTION}/granted", ""))
        return envois
