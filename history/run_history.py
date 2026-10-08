"""Construction et persistance d'un document JSON immuable par run."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4


SCHEMA_VERSION = 1
AGENTS = ("structure", "stats", "coherence", "realisme")
SEVERITES = ("critical", "high", "medium", "low")


def maintenant_utc() -> datetime:
    """Retourne un instant UTC conscient de son fuseau."""
    return datetime.now(timezone.utc)


def _en_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_utc(moment: datetime) -> str:
    return _en_utc(moment).isoformat(timespec="seconds").replace("+00:00", "Z")


def generer_run_id(moment_utc: datetime | None = None, suffixe: str | None = None) -> str:
    """Génère ``YYYYMMDDTHHMMSSZ_<8 hex>`` une seule fois par run."""
    moment = _en_utc(moment_utc or maintenant_utc())
    suffixe_unique = suffixe or uuid4().hex[:8]
    return f"{moment:%Y%m%dT%H%M%SZ}_{suffixe_unique}"


def _json_compatible(valeur):
    """Convertit récursivement les quelques objets non JSON attendus."""
    if isinstance(valeur, Path):
        return valeur.as_posix()
    if isinstance(valeur, datetime):
        return _iso_utc(valeur)
    if isinstance(valeur, dict):
        return {str(cle): _json_compatible(item) for cle, item in valeur.items()}
    if isinstance(valeur, (list, tuple, set)):
        return [_json_compatible(item) for item in valeur]
    if isinstance(valeur, float) and not math.isfinite(valeur):
        return None
    if valeur is None or isinstance(valeur, (str, int, float, bool)):
        return valeur
    return str(valeur)


def _anomalies_consolidees(verdict: dict) -> list:
    anomalies = verdict.get("anomalies_consolidees")
    if not isinstance(anomalies, list):
        anomalies = verdict.get("anomalies")
    return anomalies if isinstance(anomalies, list) else []


def _compter_anomalies_par_agent(anomalies: list) -> dict:
    """Compte chaque problème consolidé selon son type principal."""
    compteurs = dict.fromkeys(AGENTS, 0)
    correspondances = {
        "statistique": "stats",
        "statistics": "stats",
        "cohérence": "coherence",
        "realism": "realisme",
        "réalisme": "realisme",
    }
    for anomalie in anomalies:
        type_anomalie = anomalie.get("type") if isinstance(anomalie, dict) else None
        agent = correspondances.get(str(type_anomalie).lower(), str(type_anomalie).lower())
        if agent not in compteurs:
            agent = "unknown"
            compteurs.setdefault(agent, 0)
        compteurs[agent] += 1
    return compteurs


def _compter_anomalies_par_severite(anomalies: list) -> dict:
    compteurs = dict.fromkeys(SEVERITES, 0)
    for anomalie in anomalies:
        severite = anomalie.get("severite") if isinstance(anomalie, dict) else None
        severite = str(severite).lower()
        if severite not in compteurs:
            severite = "unknown"
            compteurs.setdefault(severite, 0)
        compteurs[severite] += 1
    return compteurs


def _statuts_agents(verdict: dict) -> dict:
    etapes = verdict.get("etapes_pipeline")
    etapes = etapes if isinstance(etapes, dict) else {}
    statuts = {}
    for agent in AGENTS:
        etape = etapes.get(agent)
        etape = etape if isinstance(etape, dict) else {}
        statut_brut = str(etape.get("status") or "UNKNOWN").upper()
        if statut_brut == "SUCCESS":
            statut = "SUCCESS"
        elif statut_brut in {"FAILED", "PARTIAL"}:
            statut = "FAILED"
        elif statut_brut in {"NOT_RUN", "SKIPPED"}:
            statut = "NOT_RUN"
        else:
            statut = "UNKNOWN"
        statuts[agent] = {
            "status": statut,
            "error": etape.get("error"),
        }
    return statuts


def construire_entree_fichier(
    *,
    filename: str,
    load_lock: str,
    source,
    verdict: dict,
    quarantined: bool,
    report_html=None,
    verdict_json=None,
    quarantine_key: str | None = None,
) -> dict:
    """Extrait uniquement les données finales utiles au dashboard."""
    anomalies = _anomalies_consolidees(verdict)
    references = {
        "report_html": report_html,
        "verdict_json": verdict_json,
        # Les anomalies détaillées sont incluses dans verdict_json : aucun
        # second artefact volumineux n'est créé ni dupliqué dans history.
        "anomalies_json": None,
        "source": source,
        "quarantine": quarantine_key if quarantined else None,
    }
    return _json_compatible(
        {
            "filename": filename,
            "load_lock": load_lock,
            "source": source,
            "pipeline_status": verdict.get("pipeline_status", "COMPLETE"),
            "verdict": verdict.get("verdict"),
            "score": verdict.get("score"),
            "grade": verdict.get("grade"),
            # Additive history field: the Judge already produced this text.
            # No reason is inferred when the final verdict does not contain one.
            "decision_reason": verdict.get("decision_reason") or verdict.get("justification"),
            "anomalies_total": len(anomalies),
            "anomalies_by_agent": _compter_anomalies_par_agent(anomalies),
            "agent_statuses": _statuts_agents(verdict),
            "anomalies_by_severity": _compter_anomalies_par_severite(anomalies),
            "quarantined": bool(quarantined),
            "report_html": report_html,
            "verdict_json": verdict_json,
            "quarantine_key": quarantine_key if quarantined else None,
            "artifacts": references,
            "anomaly_details": {
                "storage": "verdict_json" if verdict_json else None,
                "count": len(anomalies),
                "traceability_schema_version": (verdict.get("traceability") or {}).get("schema_version")
                if isinstance(verdict.get("traceability"), dict)
                else None,
            },
        }
    )


def _additionner_compteurs(fichiers: list, champ: str, cles: tuple) -> dict:
    total = dict.fromkeys(cles, 0)
    for fichier in fichiers:
        for cle, nombre in (fichier.get(champ) or {}).items():
            total.setdefault(cle, 0)
            total[cle] += nombre
    return total


def _score_numerique(valeur) -> float | None:
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
        return None
    score = float(valeur)
    return score if math.isfinite(score) else None


def construire_document_run(
    *,
    run_id: str,
    started_at_utc: datetime,
    finished_at_utc: datetime,
    storage_mode: str,
    files_discovered: int,
    files: list,
    artifacts: dict | None = None,
) -> dict:
    """Agrège les fichiers traités en un document historique versionné."""
    fichiers = [_json_compatible(fichier) for fichier in files]
    scores = [score for fichier in fichiers if (score := _score_numerique(fichier.get("score"))) is not None]
    summary = {
        "files_discovered": files_discovered,
        "files_processed": len(fichiers),
        "accepted": sum(f.get("pipeline_status") != "INCOMPLETE" and f.get("verdict") == "ACCEPTED" for f in fichiers),
        "warning": sum(f.get("pipeline_status") != "INCOMPLETE" and f.get("verdict") == "WARNING" for f in fichiers),
        "rejected": sum(f.get("pipeline_status") != "INCOMPLETE" and f.get("verdict") == "REJECTED" for f in fichiers),
        "incomplete": sum(f.get("pipeline_status") == "INCOMPLETE" for f in fichiers),
        "quarantined": sum(bool(f.get("quarantined")) for f in fichiers),
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
        "total_anomalies": sum(int(f.get("anomalies_total") or 0) for f in fichiers),
        "anomalies_by_agent": _additionner_compteurs(fichiers, "anomalies_by_agent", AGENTS),
        "anomalies_by_severity": _additionner_compteurs(fichiers, "anomalies_by_severity", SEVERITES),
    }
    duree = max(0.0, (_en_utc(finished_at_utc) - _en_utc(started_at_utc)).total_seconds())
    document = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at_utc": _iso_utc(started_at_utc),
        "finished_at_utc": _iso_utc(finished_at_utc),
        "duration_seconds": round(duree, 3),
        "storage_mode": storage_mode,
        "summary": summary,
        "files": fichiers,
    }
    if artifacts:
        document["artifacts"] = _json_compatible(artifacts)
    return document


def chemin_historique_local(history_dir: Path, started_at_utc: datetime, run_id: str) -> Path:
    moment = _en_utc(started_at_utc)
    return Path(history_dir) / "runs" / f"{moment:%Y}" / f"{moment:%m}" / f"{moment:%d}" / f"{run_id}.json"


def ecrire_historique_local(document: dict, history_dir: Path, started_at_utc: datetime) -> Path:
    chemin = chemin_historique_local(history_dir, started_at_utc, document["run_id"])
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(
        json.dumps(
            _json_compatible(document),
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    return chemin


def construire_prefixe_runs_s3(prefixe: str) -> str:
    """Construit l'unique racine POSIX des runs, sans doubler ``runs/``."""
    segments = [
        segment
        for segment in str(prefixe).replace("\\", "/").strip("/").split("/")
        if segment
    ]
    if not segments or segments[-1] != "runs":
        segments.append("runs")
    return PurePosixPath(*segments).as_posix().rstrip("/") + "/"


def construire_cle_historique_s3(prefixe: str, started_at_utc: datetime, run_id: str) -> str:
    moment = _en_utc(started_at_utc)
    racine_runs = construire_prefixe_runs_s3(prefixe)
    return (
        PurePosixPath(
            racine_runs,
            f"{moment:%Y}",
            f"{moment:%m}",
            f"{moment:%d}",
            f"{run_id}.json",
        ).as_posix()
    )
