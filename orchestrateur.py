"""
Orchestrateur du pipeline audit-qualite.

Le chef de gare : pour chaque fichier, enchaîne structure/ (portier, arrête
tout appel LLM si critical) -> stats/ ET Cohérence EN PARALLÈLE (chacun lit
tout le fichier ; Cohérence reçoit le df et les anomalies de structure déjà
calculés par ce module, pas de rechargement ni de double garde d'entrée) ->
Réalisme (lignes suspectes pointées par structure/stats/Cohérence + un
échantillon aléatoire de contrôle, voir _lignes_suspectes ; vérifie ses
anomalies candidates par recherche web, voir agents/regles_realisme.md) ->
judge/ (verdict) -> reporting/ (rapport HTML par fichier), puis un rapport
consolidé sur l'ensemble des fichiers traités dans cette exécution.

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
from pathlib import Path, PurePosixPath

import pandas as pd
from loguru import logger

# Permet l'exécution directe du script en plus de l'exécution en tant que module
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from agents.agent_coherence import AgentCoherence
from agents.agent_realisme import analyser_avec_statut as analyser_realisme
from agents.llm_client import quota_restant
from history.run_history import (
    construire_cle_historique_s3,
    construire_document_run,
    construire_entree_fichier,
    ecrire_historique_local,
    generer_run_id,
    maintenant_utc,
)
from ingestion.local_loader import LocalLoader
from ingestion.profiling import construire_inventaire
from ingestion.s3_client import S3Loader
from judge.agent_judge import analyser as analyser_judge
from reporting.agent_reporting import generer_rapport_consolide, generer_rapport_fichier
from stats.agent_stats import analyser as analyser_stats
from structure.agent_structure import analyser as analyser_structure
from structure.agent_structure import fichier_exploitable

logger.add(config.BASE_DIR / "audit-qualite.log", level="INFO", rotation="1 day")


def creer_loader() -> LocalLoader | S3Loader:
    """Crée le chargeur correspondant au mode de stockage configuré."""
    if config.STORAGE_MODE == "s3":
        return S3Loader()
    return LocalLoader()


def _construire_cle_rapport_s3(*parties: str) -> str:
    """Construit une clé S3 avec des séparateurs POSIX, quel que soit l'OS."""
    prefixe = str(config.S3_REPORTS_PREFIX).replace("\\", "/").strip("/")
    segments = [prefixe] if prefixe else []
    segments.extend(str(partie).replace("\\", "/").strip("/") for partie in parties)
    return PurePosixPath(*segments).as_posix()


def _construire_cle_quarantaine_s3(*parties: str) -> str:
    """Construit une clé de quarantaine S3 avec des séparateurs POSIX."""
    prefixe = str(config.S3_QUARANTINE_PREFIX).replace("\\", "/").strip("/")
    segments = [prefixe] if prefixe else []
    segments.extend(str(partie).replace("\\", "/").strip("/") for partie in parties)
    return PurePosixPath(*segments).as_posix()


def _doit_mettre_en_quarantaine(verdict: dict) -> bool:
    """Vrai pour un rejet qualité ou une analyse technique non validée."""
    return (
        verdict.get("verdict") == "REJECTED"
        or verdict.get("pipeline_status") == "INCOMPLETE"
    )


def _uploader_resultat_s3(loader, chemin_local: Path, cle_s3: str, type_resultat: str) -> bool:
    """Publie un résultat local sans propager un échec d'upload au pipeline."""
    if config.STORAGE_MODE != "s3":
        return False

    chemin_local = Path(chemin_local)
    if not chemin_local.is_file():
        logger.error(
            f"Échec upload S3 du {type_resultat.lower()} : {cle_s3} "
            f"(fichier local introuvable : {chemin_local})"
        )
        return False

    try:
        succes = loader.uploader(chemin_local, cle_s3)
    except Exception as erreur:
        logger.error(
            f"Échec upload S3 du {type_resultat.lower()} : {cle_s3} "
            f"(fichier local conservé : {chemin_local}; erreur : {erreur})",
            exc_info=True,
        )
        return False

    if succes:
        logger.info(f"{type_resultat} envoyé sur S3 : {cle_s3}")
        return True

    logger.error(
        f"Échec upload S3 du {type_resultat.lower()} : {cle_s3} "
        f"(fichier local conservé : {chemin_local})"
    )
    return False


def _mettre_en_quarantaine_s3(loader, info, verdict: dict) -> bool:
    """Copie la source en cache vers S3/quarantine sans rien supprimer."""
    if config.STORAGE_MODE != "s3" or not _doit_mettre_en_quarantaine(verdict):
        return False

    chemin_cache = Path(loader.chemin_cache(info.chemin))
    dossier_mois = info.dossier_mois or "sans_mois"
    cle_quarantaine = _construire_cle_quarantaine_s3(dossier_mois, info.nom)

    if not chemin_cache.is_file():
        logger.error(
            f"Échec mise en quarantaine S3 : {cle_quarantaine} "
            f"(fichier cache introuvable : {chemin_cache})"
        )
        return False

    try:
        succes = loader.uploader(chemin_cache, cle_quarantaine)
    except Exception as erreur:
        logger.error(
            f"Échec mise en quarantaine S3 : {cle_quarantaine} "
            f"(fichier cache conservé : {chemin_cache}; erreur : {erreur})",
            exc_info=True,
        )
        return False

    if succes:
        logger.info(f"Fichier placé en quarantaine S3 : {cle_quarantaine}")
        return True

    logger.error(
        f"Échec mise en quarantaine S3 : {cle_quarantaine} "
        f"(fichier cache conservé : {chemin_cache})"
    )
    return False


def _uploader_historique_s3(loader, chemin_local: Path, cle_s3: str) -> bool:
    """Publie l'historique sans propager un échec S3 au pipeline terminé."""
    if config.STORAGE_MODE != "s3":
        return False

    try:
        succes = loader.uploader(chemin_local, cle_s3)
    except Exception as erreur:
        logger.error(
            f"Échec upload historique S3 : {cle_s3} "
            f"(fichier local conservé : {chemin_local}; erreur : {erreur})",
            exc_info=True,
        )
        return False

    if succes:
        logger.info(f"Historique du run envoyé sur S3 : {cle_s3}")
        return True

    logger.error(
        f"Échec upload historique S3 : {cle_s3} "
        f"(fichier local conservé : {chemin_local})"
    )
    return False


def _finaliser_historique_run(
    loader,
    run_id: str,
    started_at_utc,
    files_discovered: int,
    fichiers_historique: list,
) -> dict:
    """Écrit l'historique puis conserve le chemin, la clé et le statut d'upload."""
    persistance = {
        "local_path": None,
        "s3_key": None,
        "uploaded": None,
    }
    finished_at_utc = maintenant_utc()
    try:
        document = construire_document_run(
            run_id=run_id,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            storage_mode=config.STORAGE_MODE,
            files_discovered=files_discovered,
            files=fichiers_historique,
        )
        chemin_historique = ecrire_historique_local(
            document,
            config.HISTORY_DIR,
            started_at_utc,
        )
    except Exception as erreur:
        logger.error(
            f"Échec de l'écriture de l'historique du run {run_id} : {erreur}",
            exc_info=True,
        )
        return persistance

    logger.info(f"Historique du run sauvegardé : {chemin_historique}")
    persistance["local_path"] = chemin_historique
    if config.STORAGE_MODE == "s3":
        cle_historique = construire_cle_historique_s3(
            config.S3_HISTORY_PREFIX,
            started_at_utc,
            run_id,
        )
        persistance["s3_key"] = cle_historique
        persistance["uploaded"] = _uploader_historique_s3(
            loader,
            chemin_historique,
            cle_historique,
        )
    return persistance


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


def _analyser_coherence(
    info,
    df: pd.DataFrame,
    anomalies_structure: list,
    llm_client=None,
    chemin_fichier=None,
    checkpoint_source_path=None,
) -> dict:
    """
    Lance l'agent Cohérence (agents/agent_coherence.py) sur le fichier
    complet et retourne ses anomalies avec son statut d'exécution. Canal indépendant de stats/, mais lui
    transmet le df déjà chargé et les anomalies de structure déjà calculées
    par pipeline_fichier() : elle ne recharge plus le fichier depuis le
    disque ni ne revérifie son exploitabilité une seconde fois (voir la
    docstring de AgentCoherence.analyser_fichier). Gère elle-même son
    checkpoint et sauvegarde son résultat complet dans
    resultats/coherence/<fichier>.json.

    En mode S3, ``chemin_fichier`` est le fichier téléchargé dans le
    cache local (utilisé notamment pour le SHA-256) et
    ``checkpoint_source_path`` conserve la clé S3 comme identité logique.
    """
    return AgentCoherence(llm_client=llm_client).analyser_fichier(
        chemin_fichier if chemin_fichier is not None else info.chemin,
        df=df,
        anomalies_structure=anomalies_structure,
        checkpoint_source_path=checkpoint_source_path,
    )


def _resultat_realisme_non_execute(nb_lignes: int, raison: str) -> dict:
    """Résultat explicite quand Réalisme ne peut pas démarrer."""
    return {
        "anomalies": [],
        "status": "FAILED",
        "analyse_complete": False,
        "chunks_total": 0,
        "chunks_success": 0,
        "chunks_failed": 0,
        "nb_lignes_recues": nb_lignes,
        "nb_lignes_analysees": 0,
        "nb_lignes_echec": nb_lignes,
        "error": raison,
    }


def _resume_etape(resultat: dict) -> dict:
    """Conserve dans le verdict l'état d'exécution sans dupliquer les anomalies."""
    return {cle: valeur for cle, valeur in resultat.items() if cle not in {"anomalies", "score"}}


def pipeline_fichier(
    info,
    df: pd.DataFrame,
    client=None,
    coherence_client=None,
    random_state=None,
    chemin_fichier_coherence=None,
    checkpoint_source_path=None,
) -> dict:
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

    En mode S3, ``chemin_fichier_coherence`` pointe vers le cache local et
    ``checkpoint_source_path`` contient la clé S3 d'origine. Ils restent
    facultatifs afin de conserver strictement le flux local existant.

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
        verdict = analyser_judge(nom_fichier, anomalies_structure, nb_lignes_total=len(df))
        verdict["dossier_mois"] = info.dossier_mois
        verdict["etapes_pipeline"] = {
            "structure": {
                "status": "SUCCESS",
                "analyse_complete": True,
                "error": None,
            },
            "stats": {
                "status": "NOT_RUN",
                "analyse_complete": False,
                "error": "Non exécuté : arrêt déterministe après anomalie Structure critique.",
            },
            "coherence": {
                "status": "NOT_RUN",
                "analyse_complete": False,
                "error": "Non exécuté : arrêt déterministe après anomalie Structure critique.",
            },
            "realisme": {
                "status": "NOT_RUN",
                "analyse_complete": False,
                "error": "Non exécuté : arrêt déterministe après anomalie Structure critique.",
            },
        }
        return verdict

    with ThreadPoolExecutor(max_workers=2) as executor:
        futur_stats = executor.submit(analyser_stats, nom_fichier, df)
        futur_coherence = executor.submit(
            _analyser_coherence,
            info,
            df,
            anomalies_structure,
            coherence_client,
            chemin_fichier_coherence,
            checkpoint_source_path,
        )
        anomalies_stats = futur_stats.result()
        resultat_coherence = futur_coherence.result()
        anomalies_coherence = resultat_coherence.get("anomalies", [])
    logger.info(f"  Stats : {len(anomalies_stats)} anomalie(s)")
    logger.info(
        f"  Cohérence : {len(anomalies_coherence)} anomalie(s), "
        f"statut={resultat_coherence.get('status', 'FAILED')}"
    )

    lot_suspect = _lignes_suspectes(
        df, anomalies_structure + anomalies_stats + anomalies_coherence, random_state=random_state
    )
    logger.info(f"  Réalisme : {len(lot_suspect)}/{len(df)} ligne(s) envoyée(s) (suspectes + échantillon de contrôle)")
    if lot_suspect.empty:
        resultat_realisme = analyser_realisme(
            nom_fichier, lot_suspect, client=client, echantillonner=False
        )
    elif quota_restant() <= 0:
        resultat_realisme = _resultat_realisme_non_execute(
            len(lot_suspect), "Réalisme non exécuté : quota LLM épuisé."
        )
    else:
        # L'orchestrateur a déjà choisi le lot : aucun second sampling ne
        # doit pouvoir supprimer une ligne explicitement suspecte.
        resultat_realisme = analyser_realisme(
            nom_fichier, lot_suspect, client=client, echantillonner=False
        )
    anomalies_realisme = resultat_realisme.get("anomalies", [])
    logger.info(
        f"  Réalisme : {len(anomalies_realisme)} anomalie(s), "
        f"statut={resultat_realisme.get('status', 'FAILED')}"
    )

    toutes_anomalies = anomalies_structure + anomalies_stats + anomalies_coherence + anomalies_realisme
    etapes_incompletes = [
        nom
        for nom, resultat in (("Cohérence", resultat_coherence), ("Réalisme", resultat_realisme))
        if resultat.get("status") != "SUCCESS"
    ]
    pipeline_status = "INCOMPLETE" if etapes_incompletes else "COMPLETE"
    raisons = [
        f"{nom} {resultat.get('status', 'FAILED')}: {resultat.get('error') or 'analyse incomplète'}"
        for nom, resultat in (("Cohérence", resultat_coherence), ("Réalisme", resultat_realisme))
        if resultat.get("status") != "SUCCESS"
    ]
    pipeline_reason = " ; ".join(raisons) or None

    verdict = analyser_judge(
        nom_fichier,
        toutes_anomalies,
        nb_lignes_total=len(df),
        pipeline_status=pipeline_status,
        pipeline_reason=pipeline_reason,
    )
    verdict["dossier_mois"] = info.dossier_mois
    verdict["etapes_pipeline"] = {
        "structure": {
            "status": "SUCCESS",
            "analyse_complete": True,
            "error": None,
        },
        "stats": {
            "status": "SUCCESS",
            "analyse_complete": True,
            "error": None,
        },
        "coherence": _resume_etape(resultat_coherence),
        "realisme": _resume_etape(resultat_realisme),
    }
    logger.info(
        f"  Pipeline : {pipeline_status}; verdict={verdict['verdict']} "
        f"(score={verdict['score']}, grade={verdict['grade']})"
    )

    return verdict


# ---------------------------------------------------------------------------
# Boucle sur tous les fichiers
# ---------------------------------------------------------------------------


def _fichiers_a_traiter(
    loader: LocalLoader | S3Loader,
    noms: list | None,
    dossier_mois: str | None = None,
) -> list:
    """Sélectionne les fichiers par nom et/ou LOAD_LOCK sans les charger."""
    tous = loader.lister_fichiers()
    fichiers = tous
    if dossier_mois is not None:
        fichiers = [info for info in fichiers if info.dossier_mois == dossier_mois]
    if noms:
        noms_cibles = set(noms)
        fichiers = [info for info in fichiers if info.nom in noms_cibles]
    return fichiers


def _sauvegarder_resultats_fichier(info, verdict: dict) -> dict[str, Path]:
    """Écrit les résultats locaux d'un fichier et retourne leurs chemins."""
    dossier = config.RESULTATS_DIR / (info.dossier_mois or "sans_mois")
    dossier.mkdir(parents=True, exist_ok=True)
    stem = Path(info.nom).stem

    chemin_verdict = dossier / f"verdict_{stem}.json"
    chemin_verdict.write_text(
        json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    verdict_sans_anomalies = {k: v for k, v in verdict.items() if k != "anomalies"}
    rapport = generer_rapport_fichier(verdict_sans_anomalies, verdict.get("anomalies", []))
    chemin_rapport = dossier / f"rapport_{stem}.html"
    chemin_rapport.write_text(rapport, encoding="utf-8")

    return {"verdict": chemin_verdict, "rapport": chemin_rapport}


def executer_audit(
    noms_fichiers: list | None = None,
    dossier_mois: str | None = None,
    client=None,
    coherence_client=None,
) -> dict:
    """
    Exécute un run réutilisable par le CLI et le dashboard.

    ``noms_fichiers`` limite la sélection à des noms précis et
    ``dossier_mois`` à un LOAD_LOCK. Les deux filtres peuvent être combinés.
    Une invocation produit toujours un seul ``run_id`` et un seul document
    history, y compris pour plusieurs fichiers.

    S'arrête proprement
    (sans traiter les fichiers restants) si le quota LLM quotidien est déjà
    épuisé avant un fichier : mieux vaut un run partiel visible dans les logs
    qu'un verdict ACCEPTED silencieusement faux parce que Réalisme n'a en
    réalité jamais pu être appelé.
    """
    started_at_utc = maintenant_utc()
    run_id = generer_run_id(started_at_utc)
    logger.info(f"Run {run_id} - mode de stockage actif : {config.STORAGE_MODE.upper()}")
    loader = creer_loader()

    # La sélection est établie une seule fois depuis la source active. Elle
    # devient aussi le périmètre exact du profiling : aucun scan LocalLoader
    # implicite en mode S3, ni chargement de fichiers hors de l'audit demandé.
    fichiers = _fichiers_a_traiter(loader, noms_fichiers, dossier_mois)
    files_discovered = len(fichiers)

    try:
        construire_inventaire(loader=loader, fichiers=fichiers)
        logger.info(
            f"Inventaire ciblé généré pour {files_discovered} fichier(s) "
            f"depuis le stockage {config.STORAGE_MODE.upper()}"
        )
    except Exception as erreur:
        logger.error(f"Échec du profiling préalable, audit maintenu : {erreur}", exc_info=True)

    logger.info(f"{len(fichiers)} fichier(s) à traiter")

    verdicts = []
    fichiers_historique = []
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
            options_coherence = {}
            if config.STORAGE_MODE == "s3":
                options_coherence = {
                    "chemin_fichier_coherence": loader.chemin_cache(info.chemin),
                    "checkpoint_source_path": info.chemin,
                }
            verdict = pipeline_fichier(
                info,
                df,
                client=client,
                coherence_client=coherence_client,
                **options_coherence,
            )
        except Exception as erreur:
            logger.error(f"[{i}/{len(fichiers)}] {info.nom} : échec du pipeline ({erreur})", exc_info=True)
            continue

        verdicts.append(verdict)
        chemins_resultats = _sauvegarder_resultats_fichier(info, verdict)
        chemin_rapport = chemins_resultats["rapport"]
        chemin_verdict = chemins_resultats["verdict"]
        reference_rapport = chemin_rapport
        reference_verdict = chemin_verdict
        quarantined = False
        cle_quarantaine = None

        if config.STORAGE_MODE == "s3":
            dossier_resultat = info.dossier_mois or "sans_mois"
            cle_rapport = _construire_cle_rapport_s3(dossier_resultat, chemin_rapport.name)
            cle_verdict = _construire_cle_rapport_s3(dossier_resultat, chemin_verdict.name)
            if _uploader_resultat_s3(
                loader,
                chemin_rapport,
                cle_rapport,
                "Rapport",
            ):
                reference_rapport = cle_rapport
            if _uploader_resultat_s3(
                loader,
                chemin_verdict,
                cle_verdict,
                "Verdict",
            ):
                reference_verdict = cle_verdict
            quarantined = _mettre_en_quarantaine_s3(loader, info, verdict)
            if quarantined:
                cle_quarantaine = _construire_cle_quarantaine_s3(dossier_resultat, info.nom)

        fichiers_historique.append(
            construire_entree_fichier(
                filename=info.nom,
                load_lock=info.dossier_mois,
                source=info.chemin,
                verdict=verdict,
                quarantined=quarantined,
                report_html=reference_rapport,
                verdict_json=reference_verdict,
                quarantine_key=cle_quarantaine,
            )
        )

    if verdicts:
        rapport_consolide = generer_rapport_consolide(verdicts)
        fichier_sortie = config.RESULTATS_DIR / "rapport_consolide.html"
        fichier_sortie.write_text(rapport_consolide, encoding="utf-8")
        logger.info(f"Rapport consolidé sur {len(verdicts)} fichier(s) : {fichier_sortie}")
        if config.STORAGE_MODE == "s3":
            _uploader_resultat_s3(
                loader,
                fichier_sortie,
                _construire_cle_rapport_s3(fichier_sortie.name),
                "Rapport consolidé",
            )

    persistance_historique = _finaliser_historique_run(
        loader,
        run_id,
        started_at_utc,
        files_discovered,
        fichiers_historique,
    )

    return {
        "run_id": run_id,
        "verdicts": verdicts,
        "history_path": persistance_historique["local_path"],
        "history_key": persistance_historique["s3_key"],
        "history_uploaded": persistance_historique["uploaded"],
        "files_discovered": files_discovered,
        "files_processed": len(fichiers_historique),
    }


def main(noms_fichiers: list | None = None, client=None, coherence_client=None) -> list:
    """Point d'entrée CLI historique ; retourne toujours la liste des verdicts."""
    resultat = executer_audit(
        noms_fichiers=noms_fichiers,
        client=client,
        coherence_client=coherence_client,
    )
    return resultat["verdicts"]


if __name__ == "__main__":
    main(sys.argv[1:] or None)
