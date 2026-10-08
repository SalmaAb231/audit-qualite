"""
Chargeur de données S3 pour le projet audit-qualite.

Utilisé UNIQUEMENT au déploiement (AWS EC2 + S3). En développement local,
utiliser ingestion.local_loader.LocalLoader : les deux classes exposent une
interface publique identique (lister_fichiers, charger), ce qui permet à
l'orchestrateur de basculer de l'une à l'autre en changeant une seule ligne,
par exemple :

    loader = LocalLoader()   # développement local
    loader = S3Loader()      # déploiement AWS

Aucune autre partie du pipeline ne doit avoir besoin de savoir laquelle des
deux implémentations est utilisée.
"""

from pathlib import Path

import boto3
import pandas as pd
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from loguru import logger

import config
from ingestion.local_loader import FichierInfo, LocalLoader


class S3OperationError(RuntimeError):
    """Échec S3 explicite pour les opérations génériques du dashboard."""


class S3Loader:
    """
    Accès aux fichiers de données stockés sur S3, avec cache local des
    fichiers déjà téléchargés.

    Fonctionne avec des clés AWS explicites (AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY dans .env) ou, si elles sont absentes, avec la
    chaîne de credentials par défaut de boto3 (rôle IAM attaché à l'instance
    EC2, profil partagé, variables d'environnement système, etc.).
    """

    def __init__(self, bucket: str | None = None, prefix: str | None = None, region: str | None = None):
        self.bucket = bucket or config.S3_BUCKET
        self.prefix = config.S3_INPUT_PREFIX if prefix is None else prefix

        if not self.bucket:
            logger.warning("S3_BUCKET n'est pas configuré (.env) : les appels S3Loader échoueront.")

        parametres_client = {"region_name": region or config.AWS_REGION}
        if config.AWS_ACCESS_KEY_ID and config.AWS_SECRET_ACCESS_KEY:
            parametres_client["aws_access_key_id"] = config.AWS_ACCESS_KEY_ID
            parametres_client["aws_secret_access_key"] = config.AWS_SECRET_ACCESS_KEY
        else:
            logger.info(
                "Aucune clé AWS dans .env : utilisation de la chaîne de credentials "
                "par défaut de boto3 (rôle IAM si exécuté sur EC2, profil partagé, etc.)."
            )

        self._s3 = boto3.client("s3", **parametres_client)
        # Réutilise LocalLoader pour parser les fichiers une fois en cache local
        self._loader_local = LocalLoader(data_dir=config.CACHE_DIR)

    def verifier_connexion(self) -> bool:
        """
        Vérifie que le bucket configuré est accessible avec les credentials
        actuels. N'est pas appelé par le pipeline lui-même : sert uniquement
        au script de diagnostic ingestion/test_s3_connexion.py.
        """
        if not self.bucket:
            logger.error("Aucun bucket S3 configuré (S3_BUCKET vide dans .env).")
            return False

        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except NoCredentialsError:
            logger.error("Credentials AWS manquants ou invalides : impossible d'accéder au bucket S3.")
            return False
        except ClientError as erreur:
            code = erreur.response.get("Error", {}).get("Code", "?")
            logger.error(f"Bucket S3 '{self.bucket}' inaccessible (code={code}) : {erreur}")
            return False
        except BotoCoreError as erreur:
            logger.error(f"Erreur boto3 lors de la vérification du bucket '{self.bucket}' : {erreur}")
            return False

        logger.info(f"Bucket S3 '{self.bucket}' accessible.")
        return True

    def lister_fichiers(self) -> list[FichierInfo]:
        """
        Parcourt récursivement le préfixe S3 configuré et retourne la liste
        des fichiers de données trouvés (extensions supportées uniquement),
        avec la même structure de métadonnées que LocalLoader.lister_fichiers.

        Le champ 'chemin' contient ici la clé S3 complète (str), à passer
        telle quelle à charger().
        """
        if not self.bucket:
            logger.error("Aucun bucket S3 configuré (S3_BUCKET vide dans .env) : impossible de lister les fichiers.")
            return []

        fichiers: list[FichierInfo] = []

        try:
            paginateur = self._s3.get_paginator("list_objects_v2")
            for page in paginateur.paginate(Bucket=self.bucket, Prefix=self.prefix):
                for objet in page.get("Contents", []):
                    cle = objet["Key"]
                    nom = Path(cle).name

                    if cle.endswith("/"):
                        continue
                    if Path(cle).suffix.lower() not in config.EXTENSIONS_SUPPORTEES:
                        continue
                    # Ignore les fichiers temporaires Excel (verrous d'édition, ex. ~$fichier.xlsx)
                    if nom.startswith("~$"):
                        continue

                    # Le dossier_mois est le premier segment de la clé après le préfixe
                    # (ex. LOAD_LOCK_202603), à l'image de LocalLoader avec DATA_DIR
                    cle_relative = cle[len(self.prefix):] if cle.startswith(self.prefix) else cle
                    segments = cle_relative.split("/")
                    dossier_mois = segments[0] if len(segments) > 1 else ""

                    fichiers.append(
                        FichierInfo(
                            chemin=cle,
                            nom=nom,
                            dossier_mois=dossier_mois,
                            taille_ko=round(objet["Size"] / 1024, 2),
                            date_modification=objet["LastModified"],
                        )
                    )
        except NoCredentialsError:
            logger.error("Credentials AWS manquants ou invalides : impossible de lister le bucket S3.")
            return []
        except ClientError as erreur:
            code = erreur.response.get("Error", {}).get("Code", "?")
            logger.error(f"Erreur S3 lors du listing de '{self.bucket}/{self.prefix}' (code={code}) : {erreur}")
            return []
        except BotoCoreError as erreur:
            logger.error(f"Erreur boto3 lors du listing de '{self.bucket}/{self.prefix}' : {erreur}")
            return []

        logger.info(f"{len(fichiers)} fichier(s) de données trouvé(s) sur s3://{self.bucket}/{self.prefix}")
        return fichiers

    def lister_cles(self, prefixe: str) -> list[str]:
        """Liste les clés non-dossier d'un préfixe S3 générique."""
        if not self.bucket:
            logger.error("Aucun bucket S3 configuré : impossible de lister les objets.")
            raise S3OperationError("S3 bucket is not configured")

        cles: list[str] = []
        try:
            paginateur = self._s3.get_paginator("list_objects_v2")
            for page in paginateur.paginate(Bucket=self.bucket, Prefix=prefixe):
                cles.extend(
                    objet["Key"]
                    for objet in page.get("Contents", [])
                    if not objet["Key"].endswith("/")
                )
        except NoCredentialsError:
            logger.error("Credentials AWS manquants ou invalides : listing S3 impossible.")
            raise S3OperationError(
                f"S3 listing failed for bucket={self.bucket!r}, prefix={prefixe!r}: missing credentials"
            )
        except ClientError as erreur:
            code = erreur.response.get("Error", {}).get("Code", "?")
            logger.error(f"Erreur S3 lors du listing de '{prefixe}' (code={code}) : {erreur}")
            raise S3OperationError(
                f"S3 listing failed for bucket={self.bucket!r}, prefix={prefixe!r}, code={code}"
            ) from erreur
        except BotoCoreError as erreur:
            logger.error(f"Erreur boto3 lors du listing de '{prefixe}' : {erreur}")
            raise S3OperationError(
                f"S3 listing failed for bucket={self.bucket!r}, prefix={prefixe!r}"
            ) from erreur
        return cles

    def lire_objet(self, cle: str) -> bytes:
        """Lit un objet S3 en mémoire, sans l'écrire dans le cache de données."""
        if not self.bucket:
            logger.error("Aucun bucket S3 configuré : lecture d'objet impossible.")
            raise S3OperationError("S3 bucket is not configured")
        try:
            reponse = self._s3.get_object(Bucket=self.bucket, Key=cle)
            return reponse["Body"].read()
        except NoCredentialsError:
            logger.error(f"Credentials AWS manquants ou invalides : lecture de '{cle}' impossible.")
            raise S3OperationError(
                f"S3 object read failed for bucket={self.bucket!r}, key={cle!r}: missing credentials"
            )
        except ClientError as erreur:
            code = erreur.response.get("Error", {}).get("Code", "?")
            logger.error(f"Objet S3 inaccessible '{cle}' (code={code}) : {erreur}")
            raise S3OperationError(
                f"S3 object read failed for bucket={self.bucket!r}, key={cle!r}, code={code}"
            ) from erreur
        except BotoCoreError as erreur:
            logger.error(f"Erreur boto3 lors de la lecture de '{cle}' : {erreur}")
            raise S3OperationError(
                f"S3 object read failed for bucket={self.bucket!r}, key={cle!r}"
            ) from erreur

    def chemin_cache(self, cle: str) -> Path:
        """Retourne le chemin local du cache correspondant à une clé S3."""
        return config.CACHE_DIR / str(cle)

    def source_metadata(self, cle: str) -> dict:
        """Expose le sidecar du fichier en cache sans ajouter de colonne métier."""
        return self._loader_local.source_metadata(self.chemin_cache(cle))

    def charger(self, cle: str) -> pd.DataFrame | None:
        """
        Télécharge (si nécessaire) puis charge un fichier S3 dans un DataFrame.

        Le fichier est mis en cache dans config.CACHE_DIR, à l'identique de
        l'arborescence de la clé S3. Si un fichier de même taille y est déjà
        présent, le téléchargement est sauté. Retourne None et journalise
        l'erreur en cas de problème (clé introuvable, credentials invalides,
        fichier corrompu), sans jamais lever d'exception vers l'appelant.
        """
        if not self.bucket:
            logger.error("Aucun bucket S3 configuré (S3_BUCKET vide dans .env) : impossible de charger le fichier.")
            return None

        cle = str(cle)

        try:
            entete = self._s3.head_object(Bucket=self.bucket, Key=cle)
        except NoCredentialsError:
            logger.error(f"Credentials AWS manquants ou invalides : impossible d'accéder à '{cle}'.")
            return None
        except ClientError as erreur:
            code = erreur.response.get("Error", {}).get("Code", "?")
            logger.error(f"Clé S3 introuvable ou inaccessible '{cle}' (code={code}) : {erreur}")
            return None
        except BotoCoreError as erreur:
            logger.error(f"Erreur boto3 lors de l'accès à '{cle}' : {erreur}")
            return None

        taille_distante = entete["ContentLength"]
        chemin_cache = self.chemin_cache(cle)

        if chemin_cache.exists() and chemin_cache.stat().st_size == taille_distante:
            logger.info(f"Fichier déjà en cache (même taille), téléchargement ignoré : {cle}")
        else:
            try:
                chemin_cache.parent.mkdir(parents=True, exist_ok=True)
                self._s3.download_file(self.bucket, cle, str(chemin_cache))
                logger.info(f"Téléchargé depuis S3 : s3://{self.bucket}/{cle} -> {chemin_cache}")
            except (ClientError, BotoCoreError) as erreur:
                logger.error(f"Échec du téléchargement de '{cle}' : {erreur}")
                return None

        return self._loader_local.charger(chemin_cache)

    def uploader(self, chemin_local: Path, cle_s3: str) -> bool:
        """
        Dépose un fichier local sur S3 (utilisé par l'orchestrateur pour
        publier les rapports et copier les sources en quarantaine). Retourne True en cas
        de succès, False sinon (journalisé), sans jamais lever d'exception.
        """
        chemin_local = Path(chemin_local)

        if not self.bucket:
            logger.error("Aucun bucket S3 configuré (S3_BUCKET vide dans .env) : upload annulé.")
            return False

        if not chemin_local.exists():
            logger.error(f"Fichier local introuvable, upload annulé : {chemin_local}")
            return False

        try:
            self._s3.upload_file(str(chemin_local), self.bucket, cle_s3)
        except (ClientError, BotoCoreError, NoCredentialsError) as erreur:
            logger.error(f"Échec de l'envoi de '{chemin_local}' vers s3://{self.bucket}/{cle_s3} : {erreur}")
            return False

        logger.info(f"Envoyé sur S3 : {chemin_local} -> s3://{self.bucket}/{cle_s3}")
        return True
