"""
Entraîne l'IA des variations avec Orange (équivalent du workflow graphique, en script) :

    File (dataset_variations.tab) -> Random Forest -> Test & Score -> Save Model (modele_variations.pkcls)

Usage :  python entrainer_variations.py
Le backend charge automatiquement ai/modele_variations.pkcls au démarrage (analytics.py).
"""
import os
import pickle

from Orange.classification import RandomForestLearner
from Orange.data import Table
from Orange.evaluation import CrossValidation, CA, F1

ICI = os.path.dirname(__file__)
DATASET = os.path.join(ICI, "dataset_variations.tab")
MODELE = os.path.join(ICI, "modele_variations.pkcls")


def main():
    data = Table(DATASET)
    learner = RandomForestLearner(n_estimators=100, random_state=0)
    res = CrossValidation(k=5, random_state=0)(data, [learner])
    print(f"Validation croisée (5 plis) : précision = {CA(res)[0]:.3f} | F1 = {F1(res, average='macro')[0]:.3f}")
    modele = learner(data)
    with open(MODELE, "wb") as f:
        pickle.dump(modele, f)
    print(f"✅ Modèle enregistré : {MODELE}  (classes : {list(modele.domain.class_var.values)})")


if __name__ == "__main__":
    main()
