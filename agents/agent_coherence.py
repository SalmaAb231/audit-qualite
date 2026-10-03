"""
Agent Cohérence : premier agent LLM du pipeline audit-qualite.

Détecte les contradictions logiques ENTRE COLONNES D'UNE MÊME LIGNE (ex.
motorisation électrique avec consommation en L/100km, tarif de citadine sur
un modèle premium). Canal de détection indépendant du module stats/ (canaux
parallèles, jonction au Judge dans une étape ultérieure) : lit le fichier
directement et en entier, sans mapping de schéma — l'agent s'adapte aux
colonnes réelles de chaque fichier via le prompt système (voir
agents/prompts/coherence.md).

Le détecteur de structure (structure/agent_structure.py) sert de garde
d'entrée : un fichier structurellement inexploitable n'est jamais envoyé au
LLM. structure/ n'expose pas de fonction unique 'verifier(chemin)' ; ce
module recompose le même résultat à partir de ses fonctions analyser() et
fichier_exploitable(), sans dupliquer ni modifier structure/.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from loguru import logger

import config
from agents.llm_client import AppelMetadata, LLMClient
from agents.parse_utils import valider_anomalies
from ingestion.local_loader import LocalLoader
from structure.agent_structure import analyser as _analyser_structure
from structure.agent_structure import fichier_exploitable as _fichier_exploitable

NOM_AGENT = "coherence"
CHECKPOINT_VERSION = "coherence-v1"

STATUT_SUCCESS = "SUCCESS"
STATUT_PARTIAL = "PARTIAL"
STATUT_FAILED = "FAILED"

CHEMIN_PROMPT_SYSTEME = Path(__file__).resolve().parent / "prompts" / "coherence.md"

DOSSIER_RESULTATS = config.RESULTATS_DIR / "coherence"
DOSSIER_CHECKPOINTS = DOSSIER_RESULTATS / "checkpoints"

SEED_ECHANTILLONNAGE = 42
PAUSE_ENTRE_CHUNKS_SECONDES = 3.0

# Estimation grossière du nombre de tokens d'un chunk (≈ 4 caractères par
# token), pour subdiviser dynamiquement un chunk markdown trop volumineux
# (fichiers avec un très grand nombre de colonnes).
CARACTERES_PAR_TOKEN_ESTIME = 4
TOKENS_MAX_CHUNK_ESTIMES = 30_000

# Longueur max d'une cellule dans le markdown envoyé au LLM (texte libre
# scrapé tronqué au-delà) ; voir _nettoyer_cellule.
LONGUEUR_MAX_CELLULE = 200


def _charger_prompt_systeme() -> str:
    return CHEMIN_PROMPT_SYSTEME.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Garde d'entrée : détecteur de structure de ma binôme
# ---------------------------------------------------------------------------


def _verifier_exploitabilite(nom_fichier: str, df) -> tuple[bool, str | None, list]:
    """
    Passerelle vers structure/agent_structure.py, utilisée en garde d'entrée.
    Retourne (exploitable, motif_rejet, anomalies_structure). motif_rejet est
    None si le fichier est exploitable.
    """
    anomalies_structure = _analyser_structure(nom_fichier, df)
    exploitable = _fichier_exploitable(anomalies_structure)

    motif_rejet = None
    if not exploitable:
        motifs = [a["justification"] for a in anomalies_structure if a.get("severite") == "critical"]
        motif_rejet = " ; ".join(motifs) if motifs else "Fichier jugé inexploitable par le détecteur de structure."

    return exploitable, motif_rejet, anomalies_structure


# ---------------------------------------------------------------------------
# Découpage en chunks
# ---------------------------------------------------------------------------


def _nettoyer_cellule(valeur) -> str:
    """
    Neutralise les retours à la ligne et tronque les valeurs très longues.

    Certains fichiers scrapés contiennent des cellules de texte libre avec
    des '\\n' intégrés (ex. options contractuelles détaillées) : tabulate
    les rend alors en lignes physiques multiples, ce qui fait exploser la
    taille du markdown généré (observé : x250 sur un chunk réel). Le
    nettoyage est appliqué à TOUTES les colonnes, sans supposer laquelle
    contiendra du texte long — cohérent avec l'absence de mapping de schéma.
    """
    texte = str(valeur).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(texte) > LONGUEUR_MAX_CELLULE:
        texte = texte[:LONGUEUR_MAX_CELLULE] + "…"
    return texte


def _chunk_vers_markdown(chunk_df) -> str:
    """Sérialise un chunk en markdown avec l'index d'origine en première colonne."""
    chunk_avec_index = chunk_df.reset_index().rename(columns={"index": "index_ligne"})
    chunk_nettoye = chunk_avec_index.map(_nettoyer_cellule)
    return chunk_nettoye.to_markdown(index=False)


def _estimer_tokens(texte: str) -> int:
    return len(texte) // CARACTERES_PAR_TOKEN_ESTIME


def _reduire_si_necessaire(chunk_df) -> list:
    """Subdivise récursivement un chunk dont le markdown dépasse TOKENS_MAX_CHUNK_ESTIMES."""
    if len(chunk_df) <= 1:
        return [chunk_df]

    tokens_estimes = _estimer_tokens(_chunk_vers_markdown(chunk_df))
    if tokens_estimes <= TOKENS_MAX_CHUNK_ESTIMES:
        return [chunk_df]

    logger.warning(
        f"Chunk de {len(chunk_df)} lignes estimé à {tokens_estimes} tokens "
        f"(> {TOKENS_MAX_CHUNK_ESTIMES}), subdivision en deux."
    )
    milieu = len(chunk_df) // 2
    return _reduire_si_necessaire(chunk_df.iloc[:milieu]) + _reduire_si_necessaire(chunk_df.iloc[milieu:])


def _decouper_en_chunks(df) -> list:
    """Découpe le DataFrame en chunks de config.CHUNK_SIZE lignes, réduits si trop volumineux."""
    chunks = []
    for depart in range(0, len(df), config.CHUNK_SIZE):
        chunks.append(df.iloc[depart:depart + config.CHUNK_SIZE])

    chunks_ajustes = []
    for chunk in chunks:
        chunks_ajustes.extend(_reduire_si_necessaire(chunk))
    return chunks_ajustes


def _construire_prompt_utilisateur(nom_fichier: str, chunk_df) -> str:
    return (
        f"Nom du fichier (à utiliser tel quel dans le champ 'fichier' de chaque anomalie) : {nom_fichier}\n\n"
        f"Colonnes réelles de ce fichier ({len(chunk_df.columns)}) : {list(chunk_df.columns)}\n\n"
        "La colonne 'index_ligne' indique l'index d'origine de chaque ligne dans le fichier "
        "complet : c'est cette valeur qu'il faut utiliser dans le champ 'ligne' de chaque anomalie.\n\n"
        f"Données ({len(chunk_df)} lignes) :\n\n{_chunk_vers_markdown(chunk_df)}"
    )


def _extraire_liste_anomalies(reponse) -> list:
    if isinstance(reponse, dict):
        return reponse.get("anomalies", [])
    if isinstance(reponse, list):
        return reponse
    return []


def _dedupliquer(anomalies: list) -> list:
    """Déduplique par (fichier, ligne, colonnes, type), en gardant la première occurrence."""
    vues = set()
    resultat = []
    for anomalie in anomalies:
        cle = (anomalie["fichier"], anomalie["ligne"], tuple(sorted(anomalie["colonnes"])), anomalie["type"])
        if cle in vues:
            continue
        vues.add(cle)
        resultat.append(anomalie)
    return resultat


# ---------------------------------------------------------------------------
# Checkpoints (reprise sans re-consommer de requête)
# ---------------------------------------------------------------------------


TAILLE_BLOC_HASH = 8192


def calculer_hash_fichier(chemin: str | Path) -> str:
    """Calcule le SHA-256 du contenu binaire réel d'un fichier local."""
    sha256 = hashlib.sha256()
    with Path(chemin).open("rb") as fichier:
        for bloc in iter(lambda: fichier.read(TAILLE_BLOC_HASH), b""):
            sha256.update(bloc)
    return sha256.hexdigest()


def _normaliser_source_path(source_path: str | Path) -> str:
    """Normalise un chemin relatif ou une future clé objet."""
    normalise = str(source_path).replace("\\", "/")
    while normalise.startswith("./"):
        normalise = normalise[2:]
    return normalise


def _chemin_relatif_source(chemin: str | Path) -> str:
    """Retourne un chemin local stable, relatif au projet ou à la source de données."""
    chemin_resolu = Path(chemin).resolve()

    # Cas normal : conserve notamment le préfixe data/ dans l'identité.
    racines = (config.BASE_DIR.resolve(), config.DATA_DIR.resolve().parent)
    for racine in racines:
        try:
            return chemin_resolu.relative_to(racine).as_posix()
        except ValueError:
            continue

    # Sources locales hors projet et fichiers temporaires de test : le
    # chemin stocké reste relatif, jamais un chemin Windows absolu.
    try:
        return _normaliser_source_path(
            os.path.relpath(chemin_resolu, start=config.BASE_DIR.resolve())
        )
    except ValueError as erreur:
        raise ValueError(
            "Impossible de construire un chemin source relatif stable. "
            "Fournir checkpoint_source_path explicitement."
        ) from erreur


def _creer_identite_checkpoint(
    source_path: str | Path, content_hash: str, checkpoint_version: str
) -> dict:
    """Crée une identité agnostique au stockage, réutilisable plus tard avec S3."""
    source_path_normalise = _normaliser_source_path(source_path)
    checkpoint_id = hashlib.sha256(
        f"{source_path_normalise}|{content_hash}|{checkpoint_version}".encode("utf-8")
    ).hexdigest()
    return {
        "source_path": source_path_normalise,
        "content_hash": content_hash,
        "checkpoint_version": checkpoint_version,
        "checkpoint_id": checkpoint_id,
    }


def construire_identite_checkpoint(
    chemin: str | Path,
    checkpoint_source_path: str | Path | None = None,
    checkpoint_version: str | None = None,
) -> dict:
    """
    Construit l'identité locale depuis le chemin source relatif, le SHA-256
    du contenu réel et la version explicite du format/configuration.

    ``checkpoint_source_path`` permettra à un futur chargeur distant de
    fournir une clé stable (par exemple une clé S3), sans changer le format
    de l'identité ni rendre S3 nécessaire aujourd'hui.
    """
    source_path = checkpoint_source_path or _chemin_relatif_source(chemin)
    version = checkpoint_version or CHECKPOINT_VERSION
    return _creer_identite_checkpoint(source_path, calculer_hash_fichier(chemin), version)


def _chemin_checkpoint(checkpoint_id: str) -> Path:
    return DOSSIER_CHECKPOINTS / f"{checkpoint_id}.json"


def _checkpoint_vide(identite: dict) -> dict:
    return {**identite, "chunks": {}}


def _charger_checkpoint(identite: dict) -> dict:
    chemin = _chemin_checkpoint(identite["checkpoint_id"])
    if not chemin.exists():
        return _checkpoint_vide(identite)
    try:
        checkpoint = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning(f"Checkpoint corrompu ignoré ({chemin.name}), redémarrage à zéro.")
        return _checkpoint_vide(identite)

    if (
        not isinstance(checkpoint, dict)
        or not isinstance(checkpoint.get("chunks"), dict)
        or any(checkpoint.get(cle) != valeur for cle, valeur in identite.items())
    ):
        logger.warning(f"Checkpoint incompatible ignoré ({chemin.name}), redémarrage à zéro.")
        return _checkpoint_vide(identite)
    return checkpoint


def _sauvegarder_checkpoint(checkpoint: dict) -> None:
    chemin = _chemin_checkpoint(checkpoint["checkpoint_id"])
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def _sauvegarder_resultat(nom_fichier: str, resultat: dict) -> None:
    DOSSIER_RESULTATS.mkdir(parents=True, exist_ok=True)
    chemin = DOSSIER_RESULTATS / f"{nom_fichier}.json"
    chemin.write_text(json.dumps(resultat, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Résultat sauvegardé : {chemin}")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentCoherence:
    """Détecte les incohérences logiques entre colonnes d'une même ligne, via LLM en cascade."""

    def __init__(self, llm_client: LLMClient | None = None):
        self.llm_client = llm_client or LLMClient()
        self.prompt_systeme = _charger_prompt_systeme()

    def analyser_fichier(
        self,
        chemin,
        df: pd.DataFrame | None = None,
        anomalies_structure: list | None = None,
        checkpoint_source_path: str | Path | None = None,
    ) -> dict:
        """
        Analyse un fichier : garde d'entrée (structure), puis chunks envoyés
        au LLM. Retourne le résultat agrégé et le sauvegarde dans
        resultats/coherence/<fichier>.json. Ne lève jamais d'exception :
        un échec de chargement est traité comme un rejet.

        `df` et `anomalies_structure` sont optionnels, pour un appelant qui
        a déjà chargé le fichier et déjà vérifié son exploitabilité (ex.
        orchestrateur.py, qui appelle stats/ et Cohérence en parallèle sur
        le même df déjà validé) : évite de recharger le fichier depuis le
        disque et de recalculer le garde de structure une seconde fois.
        Précondition si fournis : l'appelant a déjà vérifié
        `structure.agent_structure.fichier_exploitable(anomalies_structure)
        is True` — ce module ne revérifie pas dans ce cas. En usage
        autonome (aucun des deux fourni, ex. tests, script direct), le
        fichier est chargé et vérifié ici comme avant.

        `checkpoint_source_path` est facultatif. En local, le chemin relatif
        stable est déduit de `chemin`. Il permettra plus tard à un chargeur
        distant de fournir directement sa clé source sans changer le format
        des checkpoints.
        """
        chemin = Path(chemin)
        nom_fichier = chemin.name

        if df is None:
            df_brut = LocalLoader().charger(chemin)
            if df_brut is None:
                resultat = self._resultat_rejet(nom_fichier, "Échec du chargement du fichier (voir logs).", 0)
                _sauvegarder_resultat(nom_fichier, resultat)
                return resultat
        else:
            df_brut = df

        if anomalies_structure is None:
            exploitable, motif_rejet, anomalies_structure = _verifier_exploitabilite(nom_fichier, df_brut)
            if not exploitable:
                logger.warning(f"{nom_fichier} jugé inexploitable par le détecteur de structure : {motif_rejet}")
                resultat = self._resultat_rejet(nom_fichier, motif_rejet, len(df_brut), anomalies_structure)
                _sauvegarder_resultat(nom_fichier, resultat)
                return resultat

        nb_lignes_totales = len(df_brut)
        if nb_lignes_totales > config.SEUIL_FICHIER_GEANT:
            taille_echantillon = min(config.MAX_LIGNES_PAR_FICHIER, nb_lignes_totales)
            df = df_brut.sample(n=taille_echantillon, random_state=SEED_ECHANTILLONNAGE).sort_index()
            logger.info(
                f"{nom_fichier} : fichier géant ({nb_lignes_totales} lignes), échantillon de "
                f"{taille_echantillon} lignes (seed={SEED_ECHANTILLONNAGE})."
            )
        else:
            df = df_brut

        chunks = _decouper_en_chunks(df)
        identite_checkpoint = construire_identite_checkpoint(chemin, checkpoint_source_path)
        checkpoint = _charger_checkpoint(identite_checkpoint)

        for i, chunk_df in enumerate(chunks):
            cle = str(i)
            chunk_checkpoint = checkpoint["chunks"].get(cle)
            metadata_checkpoint = (
                chunk_checkpoint.get("metadata") if isinstance(chunk_checkpoint, dict) else None
            )
            if (
                isinstance(metadata_checkpoint, dict)
                and metadata_checkpoint.get("succes") is True
            ):
                logger.info(f"{nom_fichier} : chunk {i + 1}/{len(chunks)} déjà en checkpoint, aucun appel LLM.")
                continue

            if chunk_checkpoint is not None:
                logger.info(f"{nom_fichier} : chunk {i + 1}/{len(chunks)} en échec dans le checkpoint, nouvelle tentative.")

            anomalies_validees, meta = self._traiter_chunk(nom_fichier, chunk_df)
            checkpoint["chunks"][cle] = {
                "anomalies": anomalies_validees,
                "nb_lignes": len(chunk_df),
                "metadata": {
                    "modele": meta.modele,
                    "tokens_entree": meta.tokens_entree,
                    "tokens_sortie": meta.tokens_sortie,
                    "duree_secondes": meta.duree_secondes,
                    "nb_retries": meta.nb_retries,
                    "succes": meta.succes,
                    "depuis_cache": meta.depuis_cache,
                    "erreur": meta.erreur,
                },
            }
            _sauvegarder_checkpoint(checkpoint)
            if not meta.succes:
                logger.error(
                    f"{nom_fichier} : chunk {i + 1}/{len(chunks)} EN ÉCHEC TOTAL "
                    f"({len(chunk_df)} lignes jamais analysées, erreur : {meta.erreur})."
                )
            else:
                logger.info(
                    f"{nom_fichier} : chunk {i + 1}/{len(chunks)} traité "
                    f"({len(anomalies_validees)} anomalie(s), modèle={meta.modele})."
                )

            if i < len(chunks) - 1:
                time.sleep(PAUSE_ENTRE_CHUNKS_SECONDES)

        resultat = self._agreger(nom_fichier, checkpoint, nb_lignes_totales, len(df), len(chunks))
        _sauvegarder_resultat(nom_fichier, resultat)
        return resultat

    def _traiter_chunk(self, nom_fichier: str, chunk_df):
        prompt_utilisateur = _construire_prompt_utilisateur(nom_fichier, chunk_df)
        try:
            reponse, meta = self.llm_client.appeler(
                system_prompt=self.prompt_systeme,
                user_prompt=prompt_utilisateur,
                model=config.MODELES[NOM_AGENT],
                temperature=config.TEMPERATURES[NOM_AGENT],
                max_tokens=config.MAX_TOKENS[NOM_AGENT],
                json_attendu=True,
            )
        except Exception as erreur:
            reponse = None
            meta = AppelMetadata(
                modele="indisponible",
                succes=False,
                erreur=f"exception pendant l'appel LLM : {erreur}",
            )

        anomalies_brutes = _extraire_liste_anomalies(reponse)
        for anomalie in anomalies_brutes:
            if isinstance(anomalie, dict):
                anomalie["fichier"] = nom_fichier  # force la conformité, indépendamment du LLM

        return valider_anomalies(anomalies_brutes), meta

    @staticmethod
    def _resultat_rejet(nom_fichier: str, motif_rejet: str, nb_lignes_totales: int, anomalies: list | None = None) -> dict:
        return {
            "fichier": nom_fichier,
            "exploitable": False,
            "motif_rejet": motif_rejet,
            "status": STATUT_FAILED,
            "analyse_complete": False,
            "error": motif_rejet,
            "score": 0.0,
            "nb_lignes_analysees": 0,
            "nb_lignes_totales": nb_lignes_totales,
            "anomalies": anomalies or [],
            "metadata": {
                "modeles_utilises": [],
                "tokens_total": 0,
                "duree_totale": 0.0,
                "nb_chunks": 0,
                "nb_chunks_succes": 0,
                "nb_chunks_echec": 0,
                "nb_lignes_echec": 0,
                "nb_retries": 0,
                "cout": 0,
            },
        }

    @staticmethod
    def _agreger(nom_fichier: str, checkpoint: dict, nb_lignes_totales: int, nb_lignes_analysees: int, nb_chunks: int) -> dict:
        toutes_anomalies = []
        modeles_utilises = []
        tokens_total = 0
        duree_totale = 0.0
        nb_retries_total = 0
        nb_chunks_echec = 0
        nb_lignes_echec = 0
        erreurs = []

        for cle in sorted(checkpoint["chunks"], key=int):
            info_chunk = checkpoint["chunks"][cle]
            meta = info_chunk["metadata"]
            modeles_utilises.append(meta.get("modele"))
            tokens_total += (meta.get("tokens_entree") or 0) + (meta.get("tokens_sortie") or 0)
            duree_totale += meta.get("duree_secondes") or 0.0
            nb_retries_total += meta.get("nb_retries") or 0

            if not meta.get("succes", True):
                # Chunk en échec total (cascade + fallback épuisés) : ces lignes n'ont
                # jamais été réellement analysées, elles ne doivent pas compter comme
                # "propres" dans le score ni dans nb_lignes_analysees.
                nb_chunks_echec += 1
                nb_lignes_echec += info_chunk.get("nb_lignes", 0)
                if meta.get("erreur"):
                    erreurs.append(str(meta["erreur"]))
                continue

            toutes_anomalies.extend(info_chunk["anomalies"])

        nb_lignes_reellement_analysees = nb_lignes_analysees - nb_lignes_echec
        if nb_chunks_echec:
            logger.warning(
                f"{nom_fichier} : {nb_chunks_echec} chunk(s) en échec total, "
                f"{nb_lignes_echec} ligne(s) jamais réellement analysées."
            )

        anomalies_dedupliquees = _dedupliquer(toutes_anomalies)
        somme_ponderations = sum(config.PONDERATION_SEVERITE.get(a["severite"], 0.0) for a in anomalies_dedupliquees)
        score = 1.0 - (somme_ponderations / nb_lignes_reellement_analysees) if nb_lignes_reellement_analysees else 0.0
        score = max(0.0, min(1.0, score))

        nb_chunks_succes = max(0, nb_chunks - nb_chunks_echec)
        if nb_chunks_echec == 0:
            status = STATUT_SUCCESS
        elif nb_chunks_succes == 0:
            status = STATUT_FAILED
        else:
            status = STATUT_PARTIAL

        return {
            "fichier": nom_fichier,
            "exploitable": True,
            "motif_rejet": None,
            "status": status,
            "analyse_complete": status == STATUT_SUCCESS,
            "error": " ; ".join(dict.fromkeys(erreurs)) or None,
            "score": round(score, 4),
            "nb_lignes_analysees": nb_lignes_reellement_analysees,
            "nb_lignes_totales": nb_lignes_totales,
            "anomalies": anomalies_dedupliquees,
            "metadata": {
                "modeles_utilises": modeles_utilises,
                "tokens_total": tokens_total,
                "duree_totale": round(duree_totale, 2),
                "nb_chunks": nb_chunks,
                "nb_chunks_succes": nb_chunks_succes,
                "nb_chunks_echec": nb_chunks_echec,
                "nb_lignes_echec": nb_lignes_echec,
                "nb_retries": nb_retries_total,
                "cout": 0,
            },
        }
