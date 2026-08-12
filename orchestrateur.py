"""
Orchestrateur du pipeline audit-qualite.

Le chef de gare : pour chaque fichier, enchaîne structure/ (portier, arrête
tout appel LLM si critical) -> stats/ ET Cohérence EN PARALLÈLE (chacun lit
tout le fichier, canaux indépendants) -> Réalisme (lignes suspectes
pointées par structure/stats/Cohérence + un échantillon aléatoire de
contrôle, voir _lignes_suspectes ; connaissances internes du modèle
uniquement, PAS de recherche web — voir « Limite : pas de recherche web »
plus bas) -> judge/ (verdict) -> reporting/ (rapport HTML par fichier), puis
un rapport consolidé sur l'ensemble des fichiers traités dans cette
exécution.

Voir regles_orchestrateur.md pour les décisions de conception (sélection
des lignes suspectes, garde-fou de quota, un dossier de résultats par mois,
coût en requêtes de Cohérence sur tout le fichier).

Usage :
    python -m orchestrateur                          # tous les fichiers de DATA_DIR
    python -m orchestrateur NL_B2B_BRO_OFF.xlsx ...   # fichiers nommés (toutes les copies mensuelles trouvées)
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from loguru import logger

# Permet l'exécution directe du script en plus de l'exécution en tant que module
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from agents.agent_coherence import AgentCoherence
from agents.agent_realisme import analyser as analyser_realisme
from agents.llm_client import quota_restant
from ingestion.local_loader import LocalLoader
from judge.agent_judge import analyser as analyser_judge
from reporting.agent_reporting import generer_rapport_consolide, generer_rapport_fichier
from stats.agent_stats import analyser as analyser_stats
from structure.agent_structure import analyser as analyser_structure
from structure.agent_structure import fichier_exploitable

logger.add(config.BASE_DIR / "audit-qualite.log", level="INFO", rotation="1 day")


# ---------------------------------------------------------------------------
# Sélection des lignes envoyées à Réalisme
# ---------------------------------------------------------------------------


def _lignes_suspectes(df: pd.DataFrame, anomalies: list, random_state=None) -> pd.DataFrame:
    """
    Sous-ensemble de lignes à envoyer à Réalisme (l'agent LLM le plus coûteux
    en requêtes) : celles explicitement pointées par une anomalie de
    structure/stats, plus un échantillon aléatoire de contrôle
    (config.TAUX_ECHANTILLON_CONTROLE) parmi les lignes restantes — voir
    regles_orchestrateur.md pour la justification et les limites connues
    (verifier_outliers_numeriques ne référence que le premier outlier de
    chaque colonne, pas tous).

    N'exclut PAS `ligne == 0` : la convention "0 = portée colonne, pas une
    ligne précise" (docs/contrat_anomalie.md) entre en collision avec un
    vrai index de ligne 0, et l'exclure a caché un vrai résultat en test
    réel (le prix de 8 699 956€ injecté sur la ligne 0 de
    `NL_B2B_VLV_OFF.xlsx` n'était jamais envoyé à Réalisme). Le coût d'inclure
    à tort la ligne 0 d'une anomalie réellement column-scope est négligeable
    (une ligne de plus, une seule fois, jamais dupliquée) ; le coût de
    l'exclure à tort est de manquer un vrai problème — voir
    regles_orchestrateur.md.
    """
    index_suspects = {a["ligne"] for a in anomalies if a.get("ligne") in df.index}

    reste = df.index.difference(pd.Index(index_suspects))
    if len(reste) > 0 and config.TAUX_ECHANTILLON_CONTROLE > 0:
        echantillon = pd.Series(reste).sample(
            frac=config.TAUX_ECHANTILLON_CONTROLE, random_state=random_state
        )
        index_suspects |= set(echantillon.tolist())

    return df.loc[sorted(index_suspects)]


# ---------------------------------------------------------------------------
# Pipeline pour un seul fichier
# ---------------------------------------------------------------------------


def _analyser_coherence(info, llm_client=None) -> list:
    """
    Lance l'agent Cohérence (agents/agent_coherence.py) sur le fichier
    complet et retourne ses anomalies. Canal indépendant de stats/ : elle
    recharge elle-même le fichier depuis le disque (ne partage pas le df
    déjà chargé par l'orchestrateur) et ré-exécute son propre garde d'entrée
    structure/ — redondant avec celui de pipeline_fichier(), sans incidence
    sur le résultat (structure/ est déterministe et rapide), voir
    regles_orchestrateur.md. Gère elle-même son checkpoint et sauvegarde
    son résultat complet dans resultats/coherence/<fichier>.json.
    """
    resultat = AgentCoherence(llm_client=llm_client).analyser_fichier(info.chemin)
    return resultat.get("anomalies", [])


def pipeline_fichier(info, df: pd.DataFrame, client=None, coherence_client=None, random_state=None) -> dict:
    """
    Exécute le pipeline complet sur un fichier déjà chargé. `info` est le
    FichierInfo (ingestion.local_loader) du fichier : fournit son nom et
    son dossier_mois d'origine, ajoutés au verdict retourné. `client`
    (Réalisme) et `coherence_client` (Cohérence) sont injectables pour les
    tests (voir tests/test_orchestrateur.py) ; None en production crée un
    LLMClient réel pour chacun.

    Stats et Cohérence tournent en parallèle (deux canaux indépendants qui
    lisent chacun tout le fichier) : leurs résultats combinés alimentent la
    sélection des lignes envoyées à Réalisme et le calcul du verdict final.

    Ne lève jamais d'exception métier : une anomalie critical en Structure
    court-circuite le reste (aucun appel LLM consommé). Le verdict retourné
    porte toujours "dossier_mois" et "anomalies" (les anomalies brutes), au-delà
    du contrat de judge.agent_judge.analyser() — voir reporting/regles_reporting.md.
    """
    nom_fichier = info.nom
    logger.info(f"Pipeline sur {nom_fichier} ({info.dossier_mois})")

    anomalies_structure = analyser_structure(nom_fichier, df)
    logger.info(f"  Structure : {len(anomalies_structure)} anomalie(s)")

    if not fichier_exploitable(anomalies_structure):
        logger.warning(f"  Anomalie critical en Structure -> arrêt, pas d'appel LLM pour {nom_fichier}")
        verdict = analyser_judge(nom_fichier, anomalies_structure)
        verdict["dossier_mois"] = info.dossier_mois
        verdict["anomalies"] = anomalies_structure
        return verdict

    with ThreadPoolExecutor(max_workers=2) as executor:
        futur_stats = executor.submit(analyser_stats, nom_fichier, df)
        futur_coherence = executor.submit(_analyser_coherence, info, coherence_client)
        anomalies_stats = futur_stats.result()
        anomalies_coherence = futur_coherence.result()
    logger.info(f"  Stats : {len(anomalies_stats)} anomalie(s)")
    logger.info(f"  Cohérence : {len(anomalies_coherence)} anomalie(s)")

    lot_suspect = _lignes_suspectes(
        df, anomalies_structure + anomalies_stats + anomalies_coherence, random_state=random_state
    )
    logger.info(f"  Réalisme : {len(lot_suspect)}/{len(df)} ligne(s) envoyée(s) (suspectes + échantillon de contrôle)")
    anomalies_realisme = analyser_realisme(nom_fichier, lot_suspect, client=client)
    logger.info(f"  Réalisme : {len(anomalies_realisme)} anomalie(s)")

    toutes_anomalies = anomalies_structure + anomalies_stats + anomalies_coherence + anomalies_realisme
    verdict = analyser_judge(nom_fichier, toutes_anomalies)
    verdict["dossier_mois"] = info.dossier_mois
    verdict["anomalies"] = toutes_anomalies
    logger.info(f"  Verdict : {verdict['verdict']} (score={verdict['score']}, grade={verdict['grade']})")

    return verdict


# ---------------------------------------------------------------------------
# Boucle sur tous les fichiers
# ---------------------------------------------------------------------------


def _fichiers_a_traiter(loader: LocalLoader, noms: list | None) -> list:
    """Tous les fichiers de DATA_DIR si noms est vide/None, sinon toutes les copies mensuelles portant un de ces noms."""
    tous = loader.lister_fichiers()
    if not noms:
        return tous
    return [info for info in tous if info.nom in noms]


def _sauvegarder_resultats_fichier(info, verdict: dict) -> None:
    """Écrit verdict_<stem>.json et rapport_<stem>.html dans resultats/<dossier_mois>/ (un dossier par mois : évite toute collision entre deux fichiers de même nom dans des mois différents)."""
    dossier = config.RESULTATS_DIR / (info.dossier_mois or "sans_mois")
    dossier.mkdir(parents=True, exist_ok=True)
    stem = Path(info.nom).stem

    (dossier / f"verdict_{stem}.json").write_text(
        json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    verdict_sans_anomalies = {k: v for k, v in verdict.items() if k != "anomalies"}
    rapport = generer_rapport_fichier(verdict_sans_anomalies, verdict.get("anomalies", []))
    (dossier / f"rapport_{stem}.html").write_text(rapport, encoding="utf-8")


def main(noms_fichiers: list | None = None, client=None, coherence_client=None) -> list:
    """
    Traite les fichiers demandés (tous ceux de DATA_DIR si noms_fichiers est
    vide) et retourne la liste des verdicts produits. S'arrête proprement
    (sans traiter les fichiers restants) si le quota LLM quotidien est déjà
    épuisé avant un fichier : mieux vaut un run partiel visible dans les logs
    qu'un verdict ACCEPTED silencieusement faux parce que Réalisme n'a en
    réalité jamais pu être appelé.
    """
    loader = LocalLoader()
    fichiers = _fichiers_a_traiter(loader, noms_fichiers)
    logger.info(f"{len(fichiers)} fichier(s) à traiter")

    verdicts = []
    for i, info in enumerate(fichiers, start=1):
        if quota_restant() <= 0:
            logger.error(
                f"Quota LLM quotidien épuisé après {i - 1}/{len(fichiers)} fichier(s) : "
                f"arrêt du run, {len(fichiers) - i + 1} fichier(s) restant(s) non traité(s)."
            )
            break

        df = loader.charger(info.chemin)
        if df is None:
            logger.warning(f"[{i}/{len(fichiers)}] {info.nom} : chargement échoué, ignoré")
            continue

        try:
            verdict = pipeline_fichier(info, df, client=client, coherence_client=coherence_client)
        except Exception as erreur:
            logger.error(f"[{i}/{len(fichiers)}] {info.nom} : échec du pipeline ({erreur})", exc_info=True)
            continue

        verdicts.append(verdict)
        _sauvegarder_resultats_fichier(info, verdict)

    if verdicts:
        rapport_consolide = generer_rapport_consolide(verdicts)
        fichier_sortie = config.RESULTATS_DIR / "rapport_consolide.html"
        fichier_sortie.write_text(rapport_consolide, encoding="utf-8")
        logger.info(f"Rapport consolidé sur {len(verdicts)} fichier(s) : {fichier_sortie}")

    return verdicts


if __name__ == "__main__":
    main(sys.argv[1:] or None)
