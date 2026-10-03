"""
Script de test de l'Agent Cohérence sur 3 fichiers de référence, déjà
validés par des experts métier :
    - NL_B2B_BRO_OFF.xlsx   (petit)
    - NL_B2B_DAC_ALL.xlsx   (moyen)
    - OK_athlon_B2C_1.xlsx  (structure différente)

Ces fichiers étant déjà validés, toute anomalie signalée ici est un faux
positif probable : ce script sert à calibrer agents/prompts/coherence.md,
pas à démontrer que l'agent trouve de vraies anomalies.

Usage :
    python -m agents.test_coherence
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from agents.agent_coherence import AgentCoherence
from ingestion.local_loader import LocalLoader

FICHIERS_REFERENCE = [
    "NL_B2B_BRO_OFF.xlsx",
    "NL_B2B_DAC_ALL.xlsx",
    "OK_athlon_B2C_1.xlsx",
]

NB_ANOMALIES_AFFICHEES = 10


def _resoudre_chemin(nom_fichier: str) -> Path:
    """Recherche nom_fichier parmi tous les fichiers de DATA_DIR (tous mois confondus)."""
    loader = LocalLoader()
    for info in loader.lister_fichiers():
        if info.nom == nom_fichier:
            return info.chemin
    raise FileNotFoundError(f"Fichier de référence introuvable dans DATA_DIR : {nom_fichier}")


def _afficher_resultat(resultat: dict) -> None:
    print("\n" + "=" * 70)
    print(f"FICHIER : {resultat['fichier']}")
    print("=" * 70)
    print(f"Exploitable         : {resultat['exploitable']}")
    if not resultat["exploitable"]:
        print(f"Motif de rejet       : {resultat['motif_rejet']}")

    print(f"Score                : {resultat['score']}")
    print(f"Lignes analysées     : {resultat['nb_lignes_analysees']} / {resultat['nb_lignes_totales']}")

    anomalies = resultat["anomalies"]
    print(f"Anomalies détectées  : {len(anomalies)}")

    if anomalies:
        compte_par_severite = {}
        for anomalie in anomalies:
            compte_par_severite[anomalie["severite"]] = compte_par_severite.get(anomalie["severite"], 0) + 1
        print(f"Par sévérité         : {compte_par_severite}")

        nb_affichees = min(NB_ANOMALIES_AFFICHEES, len(anomalies))
        print(f"\n{nb_affichees} première(s) anomalie(s) :")
        for anomalie in anomalies[:NB_ANOMALIES_AFFICHEES]:
            print(f"  - ligne {anomalie['ligne']} | colonnes={anomalie['colonnes']} | valeur={anomalie['valeur']}")
            print(f"    justification : {anomalie['justification']} (confiance={anomalie['confiance']})")

    print("\nMetadata :")
    print(json.dumps(resultat["metadata"], indent=2, ensure_ascii=False))


def main() -> None:
    logger.info("Démarrage du test de l'Agent Cohérence")
    agent = AgentCoherence()

    for nom_fichier in FICHIERS_REFERENCE:
        try:
            chemin = _resoudre_chemin(nom_fichier)
        except FileNotFoundError as erreur:
            logger.error(str(erreur))
            continue

        logger.info(f"Analyse de {nom_fichier}...")
        resultat = agent.analyser_fichier(chemin)
        _afficher_resultat(resultat)

    print("\n" + "=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    print(
        "Ces fichiers sont validés experts : chaque signalement est un faux "
        "positif probable. Motifs récurrents à transformer en interdictions "
        "dans coherence.md."
    )

    logger.info("Test de l'Agent Cohérence terminé")


if __name__ == "__main__":
    main()
