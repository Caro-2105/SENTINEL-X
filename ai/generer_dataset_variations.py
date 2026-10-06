"""
Génère le jeu d'entraînement de l'IA des variations : ai/dataset_variations.tab (format natif d'Orange).

Chaque ligne = une fenêtre de 5 minutes (température, humidité, gaz) résumée par 12 variables
(voir analytics.FEATURES) + la classe : normal / anomalie / critique.

Pour chaque mesure, on tire un comportement :
  - stable                 : bruit de mesure seul                                  -> normal
  - dérive régulière       : +/- 2 à 6 %/min en permanence (ex. 5 % tout le temps) -> normal
  - saut sur UNE minute    : 2-5 % -> normal | 7-9 % -> anomalie | 11-16 % -> critique
La classe de la fenêtre = la pire des trois mesures. Les zones 5-7 % et 9-11 % sont volontairement
laissées vides : c'est le modèle qui apprend où passe la frontière.

>>> Remplacer / compléter ce fichier par de VRAIES données étiquetées dès que possible. <<<

Usage :  python generer_dataset_variations.py [nb_fenetres]
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from analytics import FEATURES, METRIQUES, extraire_features  # noqa: E402

SORTIE = os.path.join(os.path.dirname(__file__), "dataset_variations.tab")
PAS_S = 6                      # une mesure toutes les 6 s suffit à l'entraînement (le vrai système : 2 s)
BASES = {"temperature": (22.0, 2.0), "humidite": (50.0, 8.0), "gaz": (150.0, 20.0)}   # (centre, écart)
NOMS = ["normal", "anomalie", "critique"]


def niveau_saut(s):
    return 0 if s < 6 else (1 if s < 10 else 2)


def niveaux_minutes(comportement):
    """Facteur multiplicatif de chaque minute (5 minutes) et niveau de gravité du comportement."""
    taux = [0.0] * 5                                   # variation (en %) appliquée à l'entrée de chaque minute
    niveau = 0
    if comportement == "stable":
        taux = [random.gauss(0, 0.7) for _ in range(5)]
    elif comportement == "derive":
        r = random.choice([-1, 1]) * random.uniform(2, 6)
        taux = [r + random.gauss(0, 0.7) for _ in range(5)]
    else:                                              # saut
        s = random.choice([random.uniform(2, 5), random.uniform(7, 9), random.uniform(11, 16)])
        taux = [random.gauss(0, 0.7) for _ in range(5)]
        taux[random.randint(1, 4)] = random.choice([-1, 1]) * s
        niveau = niveau_saut(s)
    facteurs, f = [], 1.0
    for t in taux:
        f *= 1 + t / 100.0
        facteurs.append(f)
    return facteurs, niveau


def fenetre():
    comp = {m: random.choices(["stable", "derive", "saut"], [0.5, 0.25, 0.25])[0] for m in METRIQUES}
    series, niveau = {}, 0
    for m in METRIQUES:
        facteurs, n = niveaux_minutes(comp[m])
        niveau = max(niveau, n)
        centre = random.gauss(*BASES[m])
        series[m] = [centre * facteurs[min(4, s // 60)] * (1 + random.gauss(0, 0.003)) for s in range(0, 300, PAS_S)]
    lignes = [dict(ts=1000.0 + i * PAS_S, **{m: series[m][i] for m in METRIQUES}) for i in range(300 // PAS_S)]
    return extraire_features(lignes), niveau


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    random.seed(42)
    with open(SORTIE, "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(FEATURES + ["niveau"]) + "\n")
        f.write("\t".join(["continuous"] * len(FEATURES) + [" ".join(NOMS)]) + "\n")
        f.write("\t".join([""] * len(FEATURES) + ["class"]) + "\n")
        compte = [0, 0, 0]
        for _ in range(n):
            feats, niveau = fenetre()
            compte[niveau] += 1
            f.write("\t".join(f"{feats[k]:.3f}" for k in FEATURES) + "\t" + NOMS[niveau] + "\n")
    print(f"✅ {n} fenêtres écrites dans {SORTIE}  (normal={compte[0]}, anomalie={compte[1]}, critique={compte[2]})")


if __name__ == "__main__":
    main()
