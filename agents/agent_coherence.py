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

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

import config
from agents.llm_client import LLMClient
from agents.parse_utils import valider_anomalies
from ingestion.local_loader import LocalLoader
from structure.agent_structure import analyser as _analyser_structure
from structure.agent_structure import fichier_exploitable as _fichier_exploitable

NOM_AGENT = "coherence"

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


def _chemin_checkpoint(nom_fichier: str) -> Path:
    return DOSSIER_CHECKPOINTS / f"{nom_fichier}.json"


def _charger_checkpoint(nom_fichier: str) -> dict:
    chemin = _chemin_checkpoint(nom_fichier)
    if not chemin.exists():
        return {"chunks": {}}
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except ValueError:
        logger.warning(f"Checkpoint corrompu ignoré pour {nom_fichier}, redémarrage à zéro.")
        return {"chunks": {}}


def _sauvegarder_checkpoint(nom_fichier: str, checkpoint: dict) -> None:
    chemin = _chemin_checkpoint(nom_fichier)
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

    def analyser_fichier(self, chemin) -> dict:
        """
        Analyse un fichier : garde d'entrée (structure), puis chunks envoyés
        au LLM. Retourne le résultat agrégé et le sauvegarde dans
        resultats/coherence/<fichier>.json. Ne lève jamais d'exception :
        un échec de chargement est traité comme un rejet.
        """
        chemin = Path(chemin)
        nom_fichier = chemin.name

        df_brut = LocalLoader().charger(chemin)
        if df_brut is None:
            resultat = self._resultat_rejet(nom_fichier, "Échec du chargement du fichier (voir logs).", 0)
            _sauvegarder_resultat(nom_fichier, resultat)
            return resultat

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
        checkpoint = _charger_checkpoint(nom_fichier)

        for i, chunk_df in enumerate(chunks):
            cle = str(i)
            if cle in checkpoint["chunks"]:
                logger.info(f"{nom_fichier} : chunk {i + 1}/{len(chunks)} déjà en checkpoint, aucun appel LLM.")
                continue

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
                },
            }
            _sauvegarder_checkpoint(nom_fichier, checkpoint)
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
        reponse, meta = self.llm_client.appeler(
            system_prompt=self.prompt_systeme,
            user_prompt=prompt_utilisateur,
            model=config.MODELES[NOM_AGENT],
            temperature=config.TEMPERATURES[NOM_AGENT],
            max_tokens=config.MAX_TOKENS[NOM_AGENT],
            json_attendu=True,
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
            "score": 0.0,
            "nb_lignes_analysees": 0,
            "nb_lignes_totales": nb_lignes_totales,
            "anomalies": anomalies or [],
            "metadata": {
                "modeles_utilises": [],
                "tokens_total": 0,
                "duree_totale": 0.0,
                "nb_chunks": 0,
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

        return {
            "fichier": nom_fichier,
            "exploitable": True,
            "motif_rejet": None,
            "score": round(score, 4),
            "nb_lignes_analysees": nb_lignes_reellement_analysees,
            "nb_lignes_totales": nb_lignes_totales,
            "anomalies": anomalies_dedupliquees,
            "metadata": {
                "modeles_utilises": modeles_utilises,
                "tokens_total": tokens_total,
                "duree_totale": round(duree_totale, 2),
                "nb_chunks": nb_chunks,
                "nb_chunks_echec": nb_chunks_echec,
                "nb_lignes_echec": nb_lignes_echec,
                "nb_retries": nb_retries_total,
                "cout": 0,
            },
        }
