"""Lecture robuste de l'historique des runs en mode LOCAL ou S3."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

import config
from history.run_history import construire_prefixe_runs_s3
from ingestion.s3_client import S3Loader


@dataclass
class HistoryLoadResult:
    runs: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _normaliser_document(document, origine: str, warnings: list[str]) -> dict | None:
    if not isinstance(document, dict):
        warnings.append(f"Historique ignoré ({origine}) : objet JSON non conforme.")
        return None

    normalise = dict(document)
    if not isinstance(normalise.get("summary"), dict):
        normalise["summary"] = {}
    if not isinstance(normalise.get("files"), list):
        normalise["files"] = []
    return normalise


def _decoder_json(contenu: str | bytes, origine: str, warnings: list[str]) -> dict | None:
    try:
        if isinstance(contenu, bytes):
            contenu = contenu.decode("utf-8")
        document = json.loads(contenu)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as erreur:
        warnings.append(f"Historique JSON invalide ignoré ({origine}) : {erreur}")
        return None
    return _normaliser_document(document, origine, warnings)


def _trier_runs(runs: list[dict]) -> list[dict]:
    return sorted(
        runs,
        key=lambda run: str(run.get("started_at_utc") or ""),
        reverse=True,
    )


def load_history_runs_local(history_dir: Path | None = None) -> HistoryLoadResult:
    """Charge tous les JSON sous ``HISTORY_DIR/runs`` sans bloquer sur un fichier corrompu."""
    racine = Path(history_dir or config.HISTORY_DIR) / "runs"
    resultat = HistoryLoadResult()
    if not racine.exists():
        return resultat

    for chemin in sorted(racine.rglob("*.json")):
        try:
            contenu = chemin.read_text(encoding="utf-8")
        except OSError as erreur:
            resultat.warnings.append(f"Historique illisible ignoré ({chemin.name}) : {erreur}")
            continue
        document = _decoder_json(contenu, str(chemin), resultat.warnings)
        if document is not None:
            resultat.runs.append(document)

    resultat.runs = _trier_runs(resultat.runs)
    return resultat


def history_runs_prefix(prefixe: str | None = None) -> str:
    """Construit le préfixe POSIX ``<history>/runs/``."""
    base = config.S3_HISTORY_PREFIX if prefixe is None else prefixe
    return construire_prefixe_runs_s3(base)


def load_history_runs_s3(
    s3_loader=None,
    history_prefix: str | None = None,
) -> HistoryLoadResult:
    """Charge en une passe les JSON history S3 via l'interface publique de ``S3Loader``."""
    resultat = HistoryLoadResult()
    prefixe = history_runs_prefix(history_prefix)
    loader = s3_loader or S3Loader()
    bucket = getattr(loader, "bucket", config.S3_BUCKET) or "<not configured>"

    try:
        cles = loader.lister_cles(prefixe)
    except Exception as erreur:
        logger.error(
            f"History S3 listing failed (bucket={bucket}, prefix={prefixe}) : {erreur}",
            exc_info=True,
        )
        resultat.warnings.append(
            f"Impossible de lister l'historique S3 sous {prefixe}. "
            "Vérifiez s3:ListBucket et les logs serveur."
        )
        return resultat

    cles_json = sorted(
        str(cle)
        for cle in cles
        if not str(cle).endswith("/") and str(cle).lower().endswith(".json")
    )
    if not cles_json:
        logger.info(
            f"0 history JSON objects found under prefix {prefixe} (bucket={bucket})"
        )

    for cle in cles_json:
        try:
            contenu = loader.lire_objet(cle)
        except Exception as erreur:
            logger.error(
                f"History S3 object read failed (bucket={bucket}, key={cle}) : {erreur}",
                exc_info=True,
            )
            resultat.warnings.append(f"Objet history S3 illisible ignoré : {cle}")
            continue
        if contenu is None:
            logger.error(
                f"History S3 object read failed (bucket={bucket}, key={cle}) : empty response"
            )
            resultat.warnings.append(f"Objet history S3 illisible ignoré : {cle}")
            continue
        document = _decoder_json(contenu, cle, resultat.warnings)
        if document is not None:
            resultat.runs.append(document)

    resultat.runs = _trier_runs(resultat.runs)
    return resultat


def load_history_runs(
    storage_mode: str | None = None,
    history_dir: Path | None = None,
    s3_loader=None,
    history_prefix: str | None = None,
) -> HistoryLoadResult:
    """Point d'entrée commun utilisé par le dashboard."""
    mode = (storage_mode or config.STORAGE_MODE).lower()
    if mode == "s3":
        return load_history_runs_s3(s3_loader=s3_loader, history_prefix=history_prefix)
    return load_history_runs_local(history_dir=history_dir)


def find_run(runs: list[dict], run_id: str | None) -> dict | None:
    return next((run for run in runs if run.get("run_id") == run_id), None)
