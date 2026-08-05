"""
Script de vérification de la connexion S3.

À lancer UNIQUEMENT au moment du déploiement (AWS EC2), jamais en
développement local (qui utilise ingestion.local_loader.LocalLoader et n'a
besoin d'aucune configuration AWS).

Vérifie, dans l'ordre : que le bucket configuré est accessible avec les
credentials actuels (clés .env ou rôle IAM), puis liste les 10 premiers
fichiers trouvés sous le préfixe d'entrée (S3_INPUT_PREFIX).

Usage :
    python -m ingestion.test_s3_connexion
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

from ingestion.s3_client import S3Loader


def main() -> None:
    logger.info("Démarrage de la vérification de connexion S3")

    loader = S3Loader()

    if not loader.verifier_connexion():
        logger.error("Connexion S3 invalide, arrêt du script (voir les erreurs ci-dessus).")
        sys.exit(1)

    fichiers = loader.lister_fichiers()

    print("\n" + "=" * 60)
    print("VÉRIFICATION DE LA CONNEXION S3")
    print("=" * 60)
    print(f"Bucket              : {loader.bucket}")
    print(f"Préfixe d'entrée     : {loader.prefix}")
    print(f"Fichiers trouvés     : {len(fichiers)}")
    print("\n10 premiers fichiers :")
    if fichiers:
        for info in fichiers[:10]:
            print(f"  - {info.chemin}  ({info.taille_ko} Ko, mois={info.dossier_mois or '?'})")
    else:
        print("  (aucun fichier trouvé sous ce préfixe)")
    print("=" * 60)

    logger.info("Vérification de connexion S3 terminée")


if __name__ == "__main__":
    main()
