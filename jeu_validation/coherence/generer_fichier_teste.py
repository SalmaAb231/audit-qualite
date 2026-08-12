"""
Génère un jeu de test à vérité terrain connue pour l'Agent Cohérence.

Contexte : tous les fichiers de data/ sont déjà validés par des experts
métier (voir docs/contrat_anomalie.md) — aucune vraie incohérence n'y
existe. Cela permet de mesurer le taux de FAUX POSITIFS (agents/test_coherence.py)
mais jamais le RAPPEL de l'agent : détecte-t-il une incohérence quand il y
en a réellement une ? Sans cette mesure, un agent qui ne signale jamais rien
paraîtrait "parfait" sans être utile.

Ce script part d'un fichier déjà confirmé propre (0 faux positif :
NL_BBC_SMA_ALL.xlsx, testé le 2026-08-06) et y injecte, ligne par ligne, une
incohérence connue par axe de détection (voir agents/prompts/coherence.md).
Il produit :
- NL_BBC_SMA_ALL_avec_injections.xlsx : le fichier modifié (mêmes 198 lignes,
  1 seul chunk pour l'agent -> 1 seule requête LLM au test)
- NL_BBC_SMA_ALL_verite_terrain.json : la liste des injections attendues

Usage (ne consomme aucune requête LLM) :
    python -m jeu_validation.coherence.generer_fichier_teste
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from loguru import logger

from ingestion.local_loader import LocalLoader

FICHIER_SOURCE = "NL_BBC_SMA_ALL.xlsx"

DOSSIER_SORTIE = Path(__file__).resolve().parent
FICHIER_INJECTE = DOSSIER_SORTIE / "NL_BBC_SMA_ALL_avec_injections.xlsx"
FICHIER_VERITE = DOSSIER_SORTIE / "NL_BBC_SMA_ALL_verite_terrain.json"

# Chaque injection : (index de ligne d'origine, {colonne: nouvelle_valeur}, axe testé, description).
# Valeurs choisies à partir du contenu réel du fichier (Model='smart #1'/'smart #3',
# Fuel='ELECTRIC' uniquement, Engine='PMSM' uniquement, Power en kW/pk, Duration en
# mois, Mileage en km/an) pour rester crédibles sauf sur le point injecté.
INJECTIONS = [
    (
        10,
        {"Engine": "TDI 2.0"},
        "axe1_motorisation_vs_carburant",
        "Fuel='ELECTRIC' mais Engine='TDI 2.0' (code moteur diesel) : motorisations mutuellement exclusives.",
    ),
    (
        60,
        {"Power": "1500 kW (2039 pk)"},
        "axe3_puissance_vs_modele",
        "Puissance de 1500 kW (2039 pk) manifestement incompatible avec une smart #1/#3 (citadine électrique).",
    ),
    (
        110,
        {"Duration": 60, "Mileage": 1},
        "axe5_duree_vs_kilometrage",
        "Contrat de 60 mois avec seulement 1 km/an autorisé : incohérence durée/kilométrage.",
    ),
    (
        160,
        {"Fuel": "36"},
        "axe6_valeur_mauvaise_colonne",
        "La colonne Fuel contient '36' (une durée en mois), pas un type de carburant.",
    ),
]


def main() -> None:
    loader = LocalLoader()
    chemin_source = next((info.chemin for info in loader.lister_fichiers() if info.nom == FICHIER_SOURCE), None)
    if chemin_source is None:
        raise FileNotFoundError(f"Fichier source introuvable dans DATA_DIR : {FICHIER_SOURCE}")

    df = loader.charger(chemin_source)
    if df is None:
        raise RuntimeError(f"Échec du chargement de {FICHIER_SOURCE}")

    verite_terrain = []
    for index_ligne, modifications, axe, description in INJECTIONS:
        valeurs_avant = {colonne: df.at[index_ligne, colonne] for colonne in modifications}
        for colonne, nouvelle_valeur in modifications.items():
            df.at[index_ligne, colonne] = nouvelle_valeur

        verite_terrain.append(
            {
                "ligne": index_ligne,
                "colonnes": list(modifications.keys()),
                "axe": axe,
                "description": description,
                "valeurs_avant": {c: str(v) for c, v in valeurs_avant.items()},
                "valeurs_injectees": {c: str(v) for c, v in modifications.items()},
            }
        )
        logger.info(f"Injection ligne {index_ligne} ({axe}) : {modifications}")

    DOSSIER_SORTIE.mkdir(parents=True, exist_ok=True)
    df.to_excel(FICHIER_INJECTE, index=False)
    FICHIER_VERITE.write_text(json.dumps(verite_terrain, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(f"Fichier à injections sauvegardé : {FICHIER_INJECTE}")
    logger.info(f"Vérité terrain sauvegardée : {FICHIER_VERITE}")


if __name__ == "__main__":
    main()
