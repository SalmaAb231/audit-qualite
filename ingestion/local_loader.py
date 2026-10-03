"""
Chargeur de données local pour le projet audit-qualite.

Ce module fournit la classe LocalLoader, responsable de :
- lister récursivement les fichiers de données présents dans DATA_DIR
  (un sous-dossier par mois, ex. LOAD_LOCK_202603) ;
- charger un fichier donné (Excel ou CSV) dans un DataFrame pandas,
  en gérant proprement les erreurs de lecture.

Aucune logique métier (profiling, validation, statistiques) n'est
implémentée ici : ce module se limite à l'accès aux fichiers.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

from config import DATA_DIR, EXTENSIONS_SUPPORTEES


@dataclass
class FichierInfo:
    """Métadonnées d'un fichier de données découvert dans DATA_DIR."""

    chemin: Path
    nom: str
    dossier_mois: str
    taille_ko: float
    date_modification: datetime


class LocalLoader:
    """Accès aux fichiers de données stockés localement dans DATA_DIR."""

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = Path(data_dir)

    def lister_fichiers(self) -> list[FichierInfo]:
        """
        Parcourt récursivement les sous-dossiers de data_dir et retourne
        la liste des fichiers de données trouvés (extensions supportées
        uniquement), avec leurs métadonnées de base.
        """
        if not self.data_dir.exists():
            logger.error(f"Le dossier de données n'existe pas : {self.data_dir}")
            return []

        fichiers: list[FichierInfo] = []

        for chemin in sorted(self.data_dir.rglob("*")):
            if not chemin.is_file():
                continue
            if chemin.suffix.lower() not in EXTENSIONS_SUPPORTEES:
                continue

            # Ignore les fichiers temporaires Excel (verrous d'édition, ex. ~$fichier.xlsx)
            if chemin.name.startswith("~$"):
                continue

            stat = chemin.stat()
            # Le dossier_mois est le nom du sous-dossier direct de data_dir
            # (ex. LOAD_LOCK_202603), obtenu via le chemin relatif à data_dir
            chemin_relatif = chemin.relative_to(self.data_dir)
            dossier_mois = chemin_relatif.parts[0] if len(chemin_relatif.parts) > 1 else ""

            fichiers.append(
                FichierInfo(
                    chemin=chemin,
                    nom=chemin.name,
                    dossier_mois=dossier_mois,
                    taille_ko=round(stat.st_size / 1024, 2),
                    date_modification=datetime.fromtimestamp(stat.st_mtime),
                )
            )

        logger.info(f"{len(fichiers)} fichier(s) de données trouvé(s) dans {self.data_dir}")
        return fichiers

    def charger(self, chemin: Path) -> pd.DataFrame | None:
        """
        Charge un fichier de données (xlsx, xls ou csv) dans un DataFrame.
        Retourne None et journalise l'erreur si le fichier est illisible
        ou corrompu, sans jamais lever d'exception vers l'appelant.
        """
        chemin = Path(chemin)
        extension = chemin.suffix.lower()

        try:
            if extension in (".xlsx", ".xls"):
                return pd.read_excel(chemin)
            elif extension == ".csv":
                # Tentative avec séparateur auto-détecté, encodage UTF-8 par défaut
                return pd.read_csv(chemin, sep=None, engine="python", encoding="utf-8")
            else:
                logger.warning(f"Extension non prise en charge : {chemin}")
                return None
        except Exception as erreur:
            logger.error(f"Échec du chargement de {chemin.name} : {erreur}")
            return None
