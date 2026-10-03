"""
Script de preuve du socle LLM (LLMClient + parse_utils) sur un fichier réel,
avant l'implémentation des agents complets (Cohérence, Réalisme, Judge).

Charge un petit fichier via LocalLoader, envoie ses 20 premières lignes (avec
les noms de colonnes réels, sans aucun mapping de schéma) au modèle
"coherence", parse et valide la réponse, puis affiche et sauvegarde le tout.

Usage :
    python -m agents.test_premier_appel
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

import config
from agents.llm_client import LLMClient
from agents.parse_utils import valider_anomalies
from ingestion.local_loader import LocalLoader

NOM_FICHIER_TEST = "NL_B2B_BRO_OFF.xlsx"
NB_LIGNES_ECHANTILLON = 20
FICHIER_SORTIE = config.RESULTATS_DIR / "test_premier_appel.json"

PROMPT_SYSTEME = (
    "Tu es un auditeur de données de renting automobile. Analyse ces lignes "
    "et signale toute incohérence métier évidente. Réponds UNIQUEMENT en "
    'JSON : {"anomalies": [...]} conforme au contrat fourni.'
)


def _trouver_fichier_test() -> Path:
    """Recherche NOM_FICHIER_TEST parmi tous les fichiers de DATA_DIR (tous mois confondus)."""
    loader = LocalLoader()
    for info in loader.lister_fichiers():
        if info.nom == NOM_FICHIER_TEST:
            return info.chemin
    raise FileNotFoundError(f"Fichier de test introuvable dans {config.DATA_DIR} : {NOM_FICHIER_TEST}")


def _construire_prompt_utilisateur(echantillon) -> str:
    """Sérialise l'échantillon en markdown, colonnes réelles incluses, sans normalisation."""
    return (
        f"Colonnes du fichier ({len(echantillon.columns)}) : {list(echantillon.columns)}\n\n"
        f"Données ({len(echantillon)} lignes) :\n\n"
        f"{echantillon.to_markdown(index=False)}"
    )


def main() -> None:
    logger.info("Démarrage du test du socle LLM")

    loader = LocalLoader()
    chemin = _trouver_fichier_test()
    df = loader.charger(chemin)
    if df is None:
        raise RuntimeError(f"Échec du chargement de {chemin}")

    echantillon = df.head(NB_LIGNES_ECHANTILLON)
    prompt_utilisateur = _construire_prompt_utilisateur(echantillon)

    client = LLMClient()
    reponse_parsee, metadata = client.appeler(
        system_prompt=PROMPT_SYSTEME,
        user_prompt=prompt_utilisateur,
        model=config.MODELES["coherence"],
        temperature=config.TEMPERATURES["coherence"],
        max_tokens=config.MAX_TOKENS["coherence"],
        json_attendu=True,
    )

    anomalies_brutes = []
    if isinstance(reponse_parsee, dict):
        anomalies_brutes = reponse_parsee.get("anomalies", [])
    elif isinstance(reponse_parsee, list):
        anomalies_brutes = reponse_parsee
    anomalies_validees = valider_anomalies(anomalies_brutes)

    print("\n" + "=" * 60)
    print("RÉPONSE PARSÉE (brute, avant validation du contrat)")
    print("=" * 60)
    print(json.dumps(reponse_parsee, indent=2, ensure_ascii=False) if reponse_parsee is not None else "(aucune réponse JSON exploitable)")

    print("\n" + "=" * 60)
    print("ANOMALIES VALIDÉES")
    print("=" * 60)
    print(json.dumps(anomalies_validees, indent=2, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("METADATA DE L'APPEL")
    print("=" * 60)
    print(json.dumps(vars(metadata), indent=2, ensure_ascii=False))

    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    sortie = {
        "fichier_teste": str(chemin),
        "reponse_parsee": reponse_parsee,
        "anomalies_validees": anomalies_validees,
        "metadata": vars(metadata),
    }
    FICHIER_SORTIE.write_text(json.dumps(sortie, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Résultat complet sauvegardé dans {FICHIER_SORTIE}")


if __name__ == "__main__":
    main()
