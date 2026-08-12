"""
Agent Judge du pipeline audit-qualite.

Point de convergence du pipeline : agrège les anomalies déjà produites par
tous les détecteurs (structure, stats, cohérence, réalisme), calcule un
score de qualité et rend un verdict par fichier. 100% déterministe, aucun
appel LLM : contrairement aux détecteurs précédents, ce module ne produit
aucune anomalie, il consomme celles déjà conformes à
docs/contrat_anomalie.md — il lit uniquement les champs 'severite' et
'type', jamais 'source' (le calcul est volontairement agnostique de qui a
détecté quoi : stats, LLM, peu importe).

Formule de score : pénalité = somme, sur chaque anomalie, de son poids de
sévérité (config.PONDERATION_SEVERITE), doublé si severite == "critical"
(config.FACTEUR_PENALITE_CRITICAL). score = 1.0 - pénalité_totale, borné à
[0.0, 1.0]. Verdict et grade dérivés du même score via des seuils de
config.py (SEUIL_ACCEPTED/SEUIL_WARNING, SEUILS_GRADE) — voir
judge/regles_judge.md pour les limites découvertes en testant cette formule.

Usage :
    python -m judge.agent_judge <chemin_json_anomalies>
"""

import json
import sys
from pathlib import Path

from loguru import logger

# Permet l'exécution directe du script en plus de l'exécution en tant que module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

LIBELLES_VERDICT = {
    "ACCEPTED": "accepté",
    "WARNING": "accepté sous réserve",
    "REJECTED": "rejeté",
}


# ---------------------------------------------------------------------------
# Score, grade, verdict
# ---------------------------------------------------------------------------


SEVERITES_NON_BLOQUANTES = {"medium", "low"}


def _calculer_score(anomalies: list) -> float:
    """
    Score de qualité (0.0 à 1.0) : 1.0 moins la pénalité totale, bornée à
    [0.0, 1.0]. Pénalité = somme des poids de sévérité 'high'/'critical'
    (config.PONDERATION_SEVERITE, le poids 'critical' étant multiplié par
    config.FACTEUR_PENALITE_CRITICAL) + somme des poids 'medium'/'low',
    cette dernière plafonnée à config.PLAFOND_PENALITE_NON_BLOQUANTE.

    Le plafond évite qu'un grand nombre d'anomalies non bloquantes (voir
    docs/contrat_anomalie.md : medium = "n'empêche pas l'exploitation
    immédiate", low = "mineure ou cosmétique") ne fasse chuter le score à 0
    à elles seules — découvert en calibrant sur données réelles, où un
    détecteur purement statistique peut légitimement produire des milliers
    de ces anomalies sur un très gros fichier (voir stats/regles.md) sans
    qu'aucune ne soit individuellement grave. 'high' et 'critical' ne sont
    jamais plafonnés : une seule anomalie réellement bloquante doit pouvoir
    peser plein pot. Une sévérité absente/invalide retombe sur le poids
    'medium' (même convention de repli que agents/parse_utils.valider_anomalies),
    donc plafonnée comme 'medium'.
    """
    penalite_grave = 0.0
    penalite_non_bloquante = 0.0
    for anomalie in anomalies:
        severite = anomalie.get("severite")
        poids = config.PONDERATION_SEVERITE.get(severite, config.PONDERATION_SEVERITE["medium"])
        if severite == "critical":
            poids *= config.FACTEUR_PENALITE_CRITICAL

        if severite in SEVERITES_NON_BLOQUANTES or severite not in config.PONDERATION_SEVERITE:
            penalite_non_bloquante += poids
        else:
            penalite_grave += poids

    penalite_totale = penalite_grave + min(penalite_non_bloquante, config.PLAFOND_PENALITE_NON_BLOQUANTE)
    return max(0.0, min(1.0, 1.0 - penalite_totale))


def _grade_from_score(score: float) -> str:
    """Note de A à F : la première tranche de config.SEUILS_GRADE atteinte (ordre décroissant) l'emporte, sinon 'F'."""
    for grade, seuil in config.SEUILS_GRADE.items():
        if score >= seuil:
            return grade
    return "F"


def _verdict_from_score(score: float) -> str:
    """Verdict global selon config.SEUIL_ACCEPTED / config.SEUIL_WARNING."""
    if score >= config.SEUIL_ACCEPTED:
        return "ACCEPTED"
    if score >= config.SEUIL_WARNING:
        return "WARNING"
    return "REJECTED"


# ---------------------------------------------------------------------------
# Comptage et justification
# ---------------------------------------------------------------------------


def _compter_par_champ(anomalies: list, champ: str, valeurs_possibles: tuple) -> dict:
    """Compte les anomalies par valeur de champ (ex. 'type' ou 'severite'), toutes les valeurs possibles incluses même à 0."""
    compteur = dict.fromkeys(valeurs_possibles, 0)
    for anomalie in anomalies:
        valeur = anomalie.get(champ)
        if valeur in compteur:
            compteur[valeur] += 1
    return compteur


def _plus_severe(anomalies: list) -> dict:
    """Anomalie la plus grave (poids de sévérité puis confiance, décroissants) — citée comme exemple dans la justification."""
    return max(
        anomalies,
        key=lambda a: (config.PONDERATION_SEVERITE.get(a.get("severite"), 0.0), a.get("confiance", 0.0)),
    )


def _generer_justification(verdict: str, anomalies: list) -> str:
    """
    Justification en français, 100% déterministe (aucun appel LLM) : compte
    les anomalies par sévérité et cite la plus grave comme exemple. Un LLM
    léger pourrait reformuler ce texte à l'avenir (voir judge/regles_judge.md),
    mais n'est pas nécessaire pour une justification de cette nature.
    """
    libelle = LIBELLES_VERDICT.get(verdict, verdict)
    if not anomalies:
        return f"Fichier {libelle} : aucune anomalie détectée."

    par_severite = _compter_par_champ(anomalies, "severite", tuple(config.PONDERATION_SEVERITE.keys()))
    detail = ", ".join(f"{n} {s}" for s, n in par_severite.items() if n > 0)

    exemple = _plus_severe(anomalies)
    colonnes = ", ".join(exemple.get("colonnes") or []) or "-"

    return (
        f"Fichier {libelle} : {len(anomalies)} anomalie(s) détectée(s) ({detail}). "
        f"Exemple le plus sévère (ligne {exemple.get('ligne')}, colonne(s) {colonnes}) : "
        f"{exemple.get('justification', '')}"
    )


# ---------------------------------------------------------------------------
# Fonction principale
# ---------------------------------------------------------------------------


def analyser(nom_fichier: str, anomalies: list) -> dict:
    """
    Agrège les anomalies de tous les détecteurs et rend le verdict d'un
    fichier : score, grade, verdict et justification, 100% déterministes.
    Ne lève jamais d'exception : une liste vide ou None retourne un verdict
    ACCEPTED / grade A (aucune anomalie = aucune pénalité).
    """
    anomalies = anomalies or []

    score = _calculer_score(anomalies)
    verdict = _verdict_from_score(score)
    grade = _grade_from_score(score)

    return {
        "fichier": nom_fichier,
        "nb_anomalies_total": len(anomalies),
        "score": round(score, 4),
        "verdict": verdict,
        "grade": grade,
        "justification": _generer_justification(verdict, anomalies),
        "anomalies_par_type": _compter_par_champ(anomalies, "type", config.TYPES_ANOMALIE),
        "anomalies_par_severite": _compter_par_champ(anomalies, "severite", tuple(config.PONDERATION_SEVERITE.keys())),
    }


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage : python -m judge.agent_judge <chemin_json_anomalies>")
        sys.exit(1)

    chemin = Path(sys.argv[1])
    if not chemin.exists():
        logger.error(f"Fichier introuvable : {chemin}")
        sys.exit(1)

    contenu = json.loads(chemin.read_text(encoding="utf-8"))
    anomalies = contenu.get("anomalies", []) if isinstance(contenu, dict) else contenu

    nom_fichier = anomalies[0]["fichier"] if anomalies else chemin.stem
    verdict = analyser(nom_fichier, anomalies)

    print("\n" + "=" * 60)
    print(f"VERDICT — {nom_fichier}")
    print("=" * 60)
    print(json.dumps(verdict, indent=2, ensure_ascii=False))

    # Le verdict sauvegardé embarque aussi les anomalies brutes (pas celui
    # retourné par analyser(), dont le contrat de sortie reste inchangé) :
    # reporting/agent_reporting.py s'en sert pour le détail par anomalie
    # et le "top anomalies fréquentes" du rapport consolidé.
    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    fichier_sortie = config.RESULTATS_DIR / f"verdict_{chemin.stem}.json"
    verdict_sauvegarde = {**verdict, "anomalies": anomalies}
    fichier_sortie.write_text(json.dumps(verdict_sauvegarde, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Verdict sauvegardé dans {fichier_sortie}")


if __name__ == "__main__":
    main()
