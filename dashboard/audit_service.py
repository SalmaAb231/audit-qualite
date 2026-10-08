"""Interface Python unique entre Streamlit et l'orchestrateur."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from loguru import logger

from orchestrateur import creer_loader, executer_audit


class AuditServiceError(RuntimeError):
    """Erreur présentable dans l'UI sans exposer de détail sensible."""


@dataclass(frozen=True)
class AvailableFile:
    filename: str
    load_lock: str
    source: str
    size_kb: float | None = None
    modified_at: str | None = None


def list_available_files(loader=None) -> list[dict]:
    """Liste les entrées supportées au travers du loader actif LOCAL/S3."""
    try:
        source_loader = loader or creer_loader()
        fichiers = source_loader.lister_fichiers()
    except Exception as erreur:
        logger.error(f"Impossible de lister les fichiers d'entrée : {erreur}", exc_info=True)
        raise AuditServiceError(
            "Impossible de lister les fichiers disponibles. Consultez les logs serveur."
        ) from erreur

    resultat = []
    for info in fichiers:
        date_modification = getattr(info, "date_modification", None)
        resultat.append(
            asdict(
                AvailableFile(
                    filename=str(info.nom),
                    load_lock=str(info.dossier_mois or ""),
                    source=str(info.chemin),
                    size_kb=getattr(info, "taille_ko", None),
                    modified_at=(
                        date_modification.isoformat()
                        if hasattr(date_modification, "isoformat")
                        else str(date_modification or "") or None
                    ),
                )
            )
        )
    return sorted(resultat, key=lambda item: (item["load_lock"], item["filename"]))


def group_files_by_load_lock(files: list[dict]) -> dict[str, list[dict]]:
    groupes: dict[str, list[dict]] = {}
    for fichier in files:
        groupes.setdefault(str(fichier.get("load_lock") or ""), []).append(fichier)
    return groupes


def _run(filenames: list[str] | None, load_lock: str | None, runner=None) -> dict:
    fonction = runner or executer_audit
    try:
        resultat = fonction(noms_fichiers=filenames, dossier_mois=load_lock)
    except Exception as erreur:
        logger.error(f"Échec inattendu de l'audit : {erreur}", exc_info=True)
        raise AuditServiceError(
            "L'audit a échoué. Consultez les logs serveur pour le diagnostic."
        ) from erreur
    if not isinstance(resultat, dict) or not resultat.get("run_id"):
        logger.error("L'orchestrateur a retourné un résultat sans run_id.")
        raise AuditServiceError("L'audit s'est terminé sans résultat history identifiable.")
    return resultat


def run_single_file(filename: str, load_lock: str, runner=None) -> dict:
    if not filename or load_lock is None:
        raise AuditServiceError("Sélectionnez un LOAD_LOCK et un fichier.")
    return _run([filename], load_lock, runner=runner)


def run_selected_files(filenames: list[str], load_lock: str, runner=None) -> dict:
    if not filenames or load_lock is None:
        raise AuditServiceError("Sélectionnez un LOAD_LOCK et au moins un fichier.")
    return _run(list(filenames), load_lock, runner=runner)


def run_full_load_lock(load_lock: str, runner=None) -> dict:
    if load_lock is None:
        raise AuditServiceError("Sélectionnez un LOAD_LOCK.")
    return _run(None, load_lock, runner=runner)
