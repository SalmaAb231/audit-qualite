"""
Script d'inventaire des fichiers de données.

Par défaut, parcourt les fichiers trouvés dans DATA_DIR via LocalLoader. Un
appelant peut aussi fournir son loader actif et une sélection déjà filtrée,
notamment pour inventorier uniquement les objets concernés par un audit S3.
Le module charge chacun des fichiers retenus et produit un inventaire
(resultats/inventaire.csv) contenant,
pour chaque fichier : son nom, son mois d'origine, sa taille, son nombre de
lignes/colonnes, la liste de ses colonnes, une estimation heuristique de la
langue utilisée, et l'éventuelle erreur de lecture rencontrée.

Le script ne doit jamais s'arrêter sur un fichier corrompu : toute erreur
est capturée et notée dans la colonne 'erreur', puis le traitement continue.

Usage :
    python -m ingestion.profiling
"""

import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd
from loguru import logger
from tqdm import tqdm

# Permet l'exécution directe du script (python ingestion/profiling.py)
# en plus de l'exécution en tant que module (python -m ingestion.profiling)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import RESULTATS_DIR
from ingestion.local_loader import LocalLoader

# Fréquence de sauvegarde incrémentale (en nombre de fichiers traités)
INTERVALLE_SAUVEGARDE = 20

FICHIER_SORTIE = RESULTATS_DIR / "inventaire.csv"

# ---------------------------------------------------------------------------
# Heuristique de détection de langue
# ---------------------------------------------------------------------------

# Mots-clés fréquents observés dans des jeux de données de renting automobile,
# par langue. La détection reste volontairement simple : elle compare les
# noms de colonnes (et un échantillon de valeurs texte) à ces dictionnaires.
MOTS_CLES_LANGUE = {
    "français": {
        "nom", "prenom", "prénom", "societe", "société", "client", "date",
        "montant", "vehicule", "véhicule", "immatriculation", "contrat",
        "adresse", "ville", "code postal", "telephone", "téléphone", "email",
        "numero", "numéro", "reference", "référence", "duree", "durée",
        "kilometrage", "kilométrage", "debut", "début", "fin", "loyer",
        "assurance", "conducteur", "marque", "modele", "modèle",
    },
    "anglais": {
        "name", "date", "amount", "vehicle", "contract", "address", "city",
        "phone", "reference", "duration", "mileage", "customer", "start",
        "end", "insurance", "driver", "brand", "model", "invoice", "price",
    },
    "néerlandais": {
        "naam", "datum", "bedrag", "voertuig", "contract", "adres", "stad",
        "telefoon", "referentie", "duur", "kilometerstand", "klant",
        "begin", "einde", "verzekering", "bestuurder", "merk", "model",
        "factuur", "prijs",
    },
}


def _normaliser(texte: str) -> str:
    """Met en minuscules et retire les accents pour faciliter la comparaison."""
    texte = str(texte).strip().lower()
    texte = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in texte if not unicodedata.combining(c))


def detecter_langue(colonnes: list, df: pd.DataFrame | None) -> str:
    """
    Heuristique simple de détection de langue basée sur :
    - la correspondance des noms de colonnes avec des dictionnaires de mots-clés ;
    - un échantillon de valeurs texte du DataFrame (premières lignes).
    Retourne le nom de la langue la plus probable, ou 'indéterminé'.
    """
    scores = {langue: 0 for langue in MOTS_CLES_LANGUE}

    # Jetons issus des noms de colonnes
    jetons = set()
    for colonne in colonnes:
        jetons.add(_normaliser(colonne))

    # Ajoute un échantillon de valeurs texte (5 premières lignes, colonnes objet)
    if df is not None and not df.empty:
        try:
            colonnes_texte = df.select_dtypes(include="object").columns[:5]
            for colonne in colonnes_texte:
                for valeur in df[colonne].dropna().astype(str).head(5):
                    jetons.add(_normaliser(valeur))
        except Exception:
            pass  # L'échantillonnage est une aide facultative, jamais bloquant

    for jeton in jetons:
        for langue, mots_cles in MOTS_CLES_LANGUE.items():
            for mot in mots_cles:
                mot_normalise = _normaliser(mot)
                if mot_normalise and mot_normalise in jeton:
                    scores[langue] += 1

    meilleure_langue = max(scores, key=scores.get)
    if scores[meilleure_langue] == 0:
        return "indéterminé"
    return meilleure_langue


# ---------------------------------------------------------------------------
# Construction de l'inventaire
# ---------------------------------------------------------------------------


def construire_inventaire(loader=None, fichiers: list | None = None) -> pd.DataFrame:
    """Construit l'inventaire depuis le loader et le périmètre fournis."""
    if loader is None:
        loader = LocalLoader()
    if fichiers is None:
        fichiers = loader.lister_fichiers()
    else:
        fichiers = list(fichiers)

    if not fichiers:
        logger.warning("Aucun fichier de données trouvé, l'inventaire sera vide.")

    RESULTATS_DIR.mkdir(parents=True, exist_ok=True)

    lignes = []

    for i, info in enumerate(tqdm(fichiers, desc="Inventaire des fichiers", unit="fichier"), start=1):
        ligne = {
            "nom_fichier": info.nom,
            "dossier_mois": info.dossier_mois,
            "taille_ko": info.taille_ko,
            "nb_lignes": None,
            "nb_colonnes": None,
            "liste_colonnes": "",
            "nb_colonnes_numeriques": None,
            "langue_probable": "indéterminé",
            "erreur": "",
        }

        try:
            df = loader.charger(info.chemin)
            if df is None:
                ligne["erreur"] = "Échec du chargement du fichier (voir logs pour détail)"
            else:
                ligne["nb_lignes"] = len(df)
                ligne["nb_colonnes"] = len(df.columns)
                ligne["liste_colonnes"] = "|".join(str(c) for c in df.columns)
                ligne["nb_colonnes_numeriques"] = len(df.select_dtypes(include="number").columns)
                ligne["langue_probable"] = detecter_langue(list(df.columns), df)
        except Exception as erreur:
            logger.error(f"Erreur inattendue sur {info.nom} : {erreur}")
            ligne["erreur"] = str(erreur)

        lignes.append(ligne)

        # Sauvegarde incrémentale : protège le travail déjà fait en cas d'interruption
        if i % INTERVALLE_SAUVEGARDE == 0:
            pd.DataFrame(lignes).to_csv(FICHIER_SORTIE, index=False, encoding="utf-8-sig")
            logger.info(f"Sauvegarde incrémentale ({i}/{len(fichiers)} fichiers traités)")

    inventaire = pd.DataFrame(lignes)
    inventaire.to_csv(FICHIER_SORTIE, index=False, encoding="utf-8-sig")
    return inventaire


# ---------------------------------------------------------------------------
# Résumé affiché en fin d'exécution
# ---------------------------------------------------------------------------


def afficher_resume(inventaire: pd.DataFrame) -> None:
    """Affiche un résumé synthétique de l'inventaire dans la console."""
    total_fichiers = len(inventaire)
    total_lignes = inventaire["nb_lignes"].dropna().sum() if total_fichiers else 0
    nb_erreurs = (inventaire["erreur"] != "").sum() if total_fichiers else 0

    print("\n" + "=" * 60)
    print("RÉSUMÉ DE L'INVENTAIRE")
    print("=" * 60)
    print(f"Nombre total de fichiers   : {total_fichiers}")
    print(f"Nombre total de lignes     : {int(total_lignes)}")
    print(f"Fichiers en erreur         : {nb_erreurs}")

    print("\nRépartition par mois :")
    if total_fichiers:
        repartition = inventaire.groupby("dossier_mois").agg(
            nb_fichiers=("nom_fichier", "count"),
            nb_lignes=("nb_lignes", "sum"),
        )
        for mois, ligne in repartition.iterrows():
            print(f"  - {mois} : {int(ligne['nb_fichiers'])} fichier(s), {int(ligne['nb_lignes'])} ligne(s)")
    else:
        print("  (aucune donnée)")

    print("\nDistribution des tailles (Ko) :")
    if total_fichiers:
        tailles = inventaire["taille_ko"]
        print(f"  - min={tailles.min():.1f}  médiane={tailles.median():.1f}  "
              f"moyenne={tailles.mean():.1f}  max={tailles.max():.1f}")
    else:
        print("  (aucune donnée)")

    print("\nLangues détectées :")
    if total_fichiers:
        for langue, nb in inventaire["langue_probable"].value_counts().items():
            print(f"  - {langue} : {nb} fichier(s)")
    else:
        print("  (aucune donnée)")

    # Regroupement des fichiers par schéma identique (même liste de colonnes)
    fichiers_valides = inventaire[inventaire["erreur"] == ""]
    familles = Counter(fichiers_valides["liste_colonnes"])

    print(f"\nNombre de familles de schémas distinctes : {len(familles)}")
    print("\nTop 5 des schémas les plus fréquents :")
    for schema, nb_fichiers in familles.most_common(5):
        apercu = schema if len(schema) <= 100 else schema[:100] + "..."
        print(f"  - {nb_fichiers} fichier(s) : {apercu}")

    print("=" * 60)
    print(f"Inventaire complet sauvegardé dans : {FICHIER_SORTIE}")


def main() -> None:
    logger.info("Démarrage de l'inventaire des fichiers de données")
    inventaire = construire_inventaire()
    afficher_resume(inventaire)
    logger.info("Inventaire terminé")


if __name__ == "__main__":
    main()
