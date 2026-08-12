"""
Teste le RAPPEL de l'Agent Cohérence (complète agents/test_coherence.py, qui
mesure le taux de faux positifs sur des fichiers sans aucune anomalie
connue).

Charge le fichier à injections connues généré par
jeu_validation/coherence/generer_fichier_teste.py, lance l'agent, et compare
les anomalies détectées à la vérité terrain : chaque incohérence injectée
est-elle bien retrouvée (même ligne, au moins une colonne en commun) ?
Signale aussi toute anomalie détectée hors des lignes injectées (faux
positif sur ce fichier, puisque le reste du fichier est resté inchangé et
déjà confirmé propre).

Consomme 1 requête LLM (198 lignes = 1 chunk).

Usage :
    python -m jeu_validation.coherence.generer_fichier_teste   # une seule fois
    python -m agents.test_coherence_injection
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from agents.agent_coherence import AgentCoherence

DOSSIER_JEU = Path(__file__).resolve().parent.parent / "jeu_validation" / "coherence"
FICHIER_INJECTE = DOSSIER_JEU / "NL_BBC_SMA_ALL_avec_injections.xlsx"
FICHIER_VERITE = DOSSIER_JEU / "NL_BBC_SMA_ALL_verite_terrain.json"


def _anomalie_correspond(anomalie: dict, attendu: dict) -> bool:
    """Une anomalie détectée correspond à une injection si même ligne et au moins une colonne en commun."""
    if anomalie["ligne"] != attendu["ligne"]:
        return False
    return bool(set(anomalie["colonnes"]) & set(attendu["colonnes"]))


def main() -> None:
    if not FICHIER_INJECTE.exists() or not FICHIER_VERITE.exists():
        raise FileNotFoundError(
            "Fichier de test à injections introuvable. Lancer d'abord : "
            "python -m jeu_validation.coherence.generer_fichier_teste"
        )

    verite_terrain = json.loads(FICHIER_VERITE.read_text(encoding="utf-8"))
    logger.info(f"{len(verite_terrain)} incohérence(s) injectée(s) à retrouver")

    agent = AgentCoherence()
    resultat = agent.analyser_fichier(FICHIER_INJECTE)

    print("\n" + "=" * 70)
    print("TEST DE RAPPEL — anomalies injectées vs détectées")
    print("=" * 70)

    nb_detectees = 0
    for attendu in verite_terrain:
        correspondantes = [a for a in resultat["anomalies"] if _anomalie_correspond(a, attendu)]
        statut = "DÉTECTÉE" if correspondantes else "MANQUÉE"
        if correspondantes:
            nb_detectees += 1

        print(f"\n[{statut}] ligne {attendu['ligne']} ({attendu['axe']})")
        print(f"  Attendu : {attendu['description']}")
        for anomalie in correspondantes:
            print(f"  Détecté : colonnes={anomalie['colonnes']} | {anomalie['justification']} (confiance={anomalie['confiance']})")

    autres = [
        anomalie
        for anomalie in resultat["anomalies"]
        if not any(_anomalie_correspond(anomalie, attendu) for attendu in verite_terrain)
    ]

    print("\n" + "=" * 70)
    print(f"RAPPEL : {nb_detectees}/{len(verite_terrain)} incohérences injectées détectées")
    print(f"FAUX POSITIFS ADDITIONNELS (lignes non modifiées, donc réellement propres) : {len(autres)}")
    print("=" * 70)
    for anomalie in autres:
        print(f"  - ligne {anomalie['ligne']} | colonnes={anomalie['colonnes']} | {anomalie['justification']}")

    logger.info("Test de rappel terminé")


if __name__ == "__main__":
    main()
