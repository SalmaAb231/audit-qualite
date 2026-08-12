"""
Second jeu de test à vérité terrain connue pour l'Agent Cohérence.

Complète generer_fichier_teste.py (qui couvre les axes 1, 3, 5, 6) avec les
2 axes de détection restants :
- axe 2 (prix/loyer vs catégorie et modèle)
- axe 4 (capacités physiques vs modèle)

Le fichier NL_BBC_SMA_ALL.xlsx (base du premier jeu) ne contient ni colonne
de capacité physique ni diversité de modèles/catégories suffisante pour ces
deux axes. On part donc d'un petit extrait de NL_B2B_DAC_ALL.xlsx (déjà
confirmé propre en v1.2), qui a une colonne `Seats` et plusieurs modèles de
gammes différentes (SPRING = citadine électrique 4 places, BIGSTER = SUV
familial).

Produit :
- NL_B2B_DAC_ALL_extrait_avec_injections.xlsx (80 lignes, 1 seul chunk)
- NL_B2B_DAC_ALL_extrait_verite_terrain.json

Usage (ne consomme aucune requête LLM) :
    python -m jeu_validation.coherence.generer_fichier_teste_2
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
from loguru import logger

from ingestion.local_loader import LocalLoader

FICHIER_SOURCE = "NL_B2B_DAC_ALL.xlsx"

DOSSIER_SORTIE = Path(__file__).resolve().parent
FICHIER_INJECTE = DOSSIER_SORTIE / "NL_B2B_DAC_ALL_extrait_avec_injections.xlsx"
FICHIER_VERITE = DOSSIER_SORTIE / "NL_B2B_DAC_ALL_extrait_verite_terrain.json"

# Deux plages d'index d'origine choisies pour inclure au moins une ligne
# SPRING (citadine électrique 4 places) et une ligne BIGSTER (SUV familial),
# indispensables pour des injections crédibles sur les axes 2 et 4.
PLAGES_EXTRAIT = [(0, 40), (601, 641)]

# Chaque injection est exprimée en index D'ORIGINE (dans NL_B2B_DAC_ALL.xlsx
# complet) : _vers_position_finale() les convertit en position dans le
# fichier extrait concaténé (l'ordre des lignes change avec pd.concat, donc
# la position finale d'une ligne n'est pas son index d'origine).
INJECTIONS_INDEX_ORIGINE = [
    (
        10,
        {"Seats": "7 zitplaatsen"},
        "axe4_capacites_physiques_vs_modele",
        "La Dacia SPRING est une citadine électrique 4 places : jamais commercialisée en version 7 places.",
    ),
    (
        605,
        {"Rental": 15},
        "axe2_prix_vs_categorie_modele",
        "Un loyer de 15 €/mois pour un SUV familial (BIGSTER, finition 'expression') est implausible pour ce segment.",
    ),
]


def _vers_position_finale(index_origine: int) -> int:
    """Convertit un index de ligne d'origine (NL_B2B_DAC_ALL.xlsx complet) en position dans l'extrait concaténé."""
    position = 0
    for debut, fin in PLAGES_EXTRAIT:
        if debut <= index_origine < fin:
            return position + (index_origine - debut)
        position += fin - debut
    raise ValueError(f"Index d'origine {index_origine} hors des plages extraites {PLAGES_EXTRAIT}")


def main() -> None:
    loader = LocalLoader()
    chemin_source = next((info.chemin for info in loader.lister_fichiers() if info.nom == FICHIER_SOURCE), None)
    if chemin_source is None:
        raise FileNotFoundError(f"Fichier source introuvable dans DATA_DIR : {FICHIER_SOURCE}")

    df_complet = loader.charger(chemin_source)
    if df_complet is None:
        raise RuntimeError(f"Échec du chargement de {FICHIER_SOURCE}")

    df = pd.concat([df_complet.iloc[debut:fin] for debut, fin in PLAGES_EXTRAIT]).reset_index(drop=True)

    verite_terrain = []
    for index_origine, modifications, axe, description in INJECTIONS_INDEX_ORIGINE:
        position_finale = _vers_position_finale(index_origine)
        valeurs_avant = {colonne: df.at[position_finale, colonne] for colonne in modifications}
        for colonne, nouvelle_valeur in modifications.items():
            df.at[position_finale, colonne] = nouvelle_valeur

        verite_terrain.append(
            {
                "ligne": position_finale,
                "colonnes": list(modifications.keys()),
                "axe": axe,
                "description": description,
                "valeurs_avant": {c: str(v) for c, v in valeurs_avant.items()},
                "valeurs_injectees": {c: str(v) for c, v in modifications.items()},
            }
        )
        logger.info(f"Injection ligne d'origine {index_origine} -> position {position_finale} ({axe}) : {modifications}")

    DOSSIER_SORTIE.mkdir(parents=True, exist_ok=True)
    df.to_excel(FICHIER_INJECTE, index=False)
    FICHIER_VERITE.write_text(json.dumps(verite_terrain, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(f"Fichier à injections sauvegardé : {FICHIER_INJECTE} ({len(df)} lignes)")
    logger.info(f"Vérité terrain sauvegardée : {FICHIER_VERITE}")


if __name__ == "__main__":
    main()
