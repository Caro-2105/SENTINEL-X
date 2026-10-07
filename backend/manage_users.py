"""
Gestion des mots de passe du tableau de bord SENTINEL-X (aucun mot de passe n'est écrit dans le code).

    python manage_users.py mdp Caroline     # définit / change le mot de passe (saisie masquée)
    python manage_users.py liste            # membres et présence d'un mot de passe

Le nom doit être identique au label du modèle de reconnaissance (Caroline, Florent, Killian).
"""
import getpass
import sys

import auth
import db


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "liste":
        for label, actif, a_mdp in db.list_membres_auth():
            print(f"{label:<15} {'actif' if actif else 'INACTIF':<8} mot de passe : {'oui' if a_mdp else 'NON'}")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "mdp":
        label = sys.argv[2]
        mdp = getpass.getpass(f"Nouveau mot de passe pour {label} : ")
        probleme = auth.verifier_force(mdp)
        if probleme:
            print(f"❌ Mot de passe refusé : {probleme}.")
            raise SystemExit(1)
        if mdp != getpass.getpass("Confirmer : "):
            print("❌ Les deux saisies sont différentes.")
            raise SystemExit(1)
        db.init_db()
        if db.set_mot_de_passe_hash(label, auth.hash_password(mdp)):
            print(f"✅ Mot de passe de {label} enregistré (haché).")
        else:
            print("❌ Base de données injoignable (docker compose up -d ?).")
            raise SystemExit(1)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
