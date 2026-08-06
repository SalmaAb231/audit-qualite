"""
Client HTTP pour l'API OpenRouter, socle commun à tous les agents LLM du
pipeline (Cohérence, Réalisme, Judge, Reporting).

Centralise l'appel réseau, la cascade de modèles (config.MODELES : liste par
agent, du principal aux fallbacks, puis config.MODELE_FALLBACK en tout
dernier recours), les retries progressifs, le cache des réponses et le
respect des contraintes du tier gratuit OpenRouter (config.RATE_LIMIT),
afin qu'un agent n'ait qu'à fournir ses prompts et récupérer une réponse
déjà robustifiée, sans dupliquer cette logique.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from loguru import logger

import config
from agents.parse_utils import extraire_json

# Utilisés quand l'appelant ne précise pas explicitement de température/
# max_tokens (les agents passeront normalement les valeurs de config.py).
TEMPERATURE_PAR_DEFAUT = 0.0
MAX_TOKENS_PAR_DEFAUT = 1024

ENDPOINT_CHAT_COMPLETIONS = "/chat/completions"

# Codes HTTP considérés comme transitoires, donc retentables
CODES_HTTP_RETRYABLE = {429}


@dataclass
class AppelMetadata:
    """Métadonnées d'un appel LLM, retournées en complément de la réponse parsée."""

    modele: str
    succes: bool = False
    tokens_entree: int | None = None
    tokens_sortie: int | None = None
    duree_secondes: float = 0.0
    nb_retries: int = 0
    modele_fallback_utilise: bool = False
    cout_estime: float | None = None
    depuis_cache: bool = False
    erreur: str | None = None


# ---------------------------------------------------------------------------
# Cache des réponses LLM (config.CACHE_LLM_DIR) : un prompt identique
# (même system/user/température/max_tokens) est servi depuis le disque sans
# consommer de requête OpenRouter.
# ---------------------------------------------------------------------------


def _cle_cache(system_prompt: str, user_prompt: str, temperature: float, max_tokens: int, json_attendu: bool) -> str:
    empreinte = "\x1f".join([system_prompt, user_prompt, str(temperature), str(max_tokens), str(json_attendu)])
    return hashlib.sha256(empreinte.encode("utf-8")).hexdigest()


def _lire_cache(cle: str) -> dict | None:
    chemin = config.CACHE_LLM_DIR / f"{cle}.json"
    if not chemin.exists():
        return None
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except ValueError:
        logger.warning(f"Entrée de cache LLM corrompue ignorée : {chemin.name}")
        return None


def _ecrire_cache(cle: str, modele: str, texte: str, tokens_entree: int | None, tokens_sortie: int | None, cout_estime: float | None) -> None:
    config.CACHE_LLM_DIR.mkdir(parents=True, exist_ok=True)
    chemin = config.CACHE_LLM_DIR / f"{cle}.json"
    chemin.write_text(
        json.dumps(
            {
                "modele": modele,
                "texte": texte,
                "tokens_entree": tokens_entree,
                "tokens_sortie": tokens_sortie,
                "cout_estime": cout_estime,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Compteur quotidien de requêtes réelles (config.COMPTEUR_REQUETES_FICHIER),
# pour ne jamais dépasser config.RATE_LIMIT["max_requetes_jour"].
# ---------------------------------------------------------------------------


def _lire_compteur_quotidien() -> int:
    """Retourne le nombre de requêtes déjà effectuées aujourd'hui (0 si nouveau jour ou fichier absent/corrompu)."""
    try:
        donnees = json.loads(config.COMPTEUR_REQUETES_FICHIER.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return 0
    if donnees.get("date") != date.today().isoformat():
        return 0
    return donnees.get("compteur", 0)


def _incrementer_compteur_quotidien() -> int:
    """Incrémente et persiste le compteur quotidien de requêtes réelles. Retourne le nouveau total."""
    compteur = _lire_compteur_quotidien() + 1
    config.CACHE_LLM_DIR.mkdir(parents=True, exist_ok=True)
    config.COMPTEUR_REQUETES_FICHIER.write_text(
        json.dumps({"date": date.today().isoformat(), "compteur": compteur}),
        encoding="utf-8",
    )
    return compteur


class LLMClient:
    """
    Client d'appel à l'API OpenRouter, avec cascade de modèles, retries
    progressifs, cache de réponses et respect des limites du tier gratuit.
    """

    # Horodatage (time.monotonic) du dernier appel réseau réel, partagé par
    # toutes les instances du processus, pour respecter RATE_LIMIT["pause_entre_appels"]
    # même si plusieurs LLMClient() sont créés (un par agent, par exemple).
    _dernier_appel_ts: float | None = None

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.base_url = (base_url or config.OPENROUTER_BASE_URL).rstrip("/")
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY est vide : les appels LLM échoueront.")

    def appeler(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | list[str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_attendu: bool = True,
    ) -> tuple[Any, AppelMetadata]:
        """
        Envoie un prompt système + utilisateur à une cascade de modèles OpenRouter.

        `model` peut être un identifiant unique, ou (cas normal avec
        config.MODELES) une liste ordonnée : le premier modèle est essayé
        avec jusqu'à config.RATE_LIMIT["retries_max"] retries progressifs
        (délais doublés depuis config.RATE_LIMIT["backoff_initial"]) sur
        erreur réseau, HTTP 429/5xx ou JSON invalide (si json_attendu). Une
        erreur HTTP non transitoire (4xx hors 429) passe immédiatement au
        modèle suivant. Si toute la liste échoue, un dernier essai est fait
        avec config.MODELE_FALLBACK (l'auto-routeur du tier gratuit).

        Si un appel strictement identique (mêmes prompts/température/
        max_tokens) a déjà réussi, la réponse est servie depuis
        config.CACHE_LLM_DIR sans consommer de requête. Sinon, chaque
        tentative réseau réelle est comptée dans config.COMPTEUR_REQUETES_FICHIER
        et bloquée proprement si config.RATE_LIMIT["max_requetes_jour"] est atteint.

        Retourne (reponse_parsee, metadata). Si json_attendu, reponse_parsee
        est le JSON extrait (dict/list) en cas de succès, ou la meilleure
        réponse partielle récupérée sinon (peut être None). Si json_attendu
        est faux, reponse_parsee est le texte brut de la réponse.
        """
        temperature = TEMPERATURE_PAR_DEFAUT if temperature is None else temperature
        max_tokens = MAX_TOKENS_PAR_DEFAUT if max_tokens is None else max_tokens

        cle_cache = _cle_cache(system_prompt, user_prompt, temperature, max_tokens, json_attendu)
        entree_cache = _lire_cache(cle_cache)
        if entree_cache is not None:
            logger.info(f"Réponse servie depuis le cache LLM (modèle d'origine={entree_cache['modele']}), aucune requête consommée.")
            reponse = extraire_json(entree_cache["texte"]) if json_attendu else entree_cache["texte"]
            metadata = AppelMetadata(
                modele=entree_cache["modele"],
                succes=True,
                tokens_entree=entree_cache.get("tokens_entree"),
                tokens_sortie=entree_cache.get("tokens_sortie"),
                cout_estime=entree_cache.get("cout_estime"),
                depuis_cache=True,
            )
            return reponse, metadata

        if isinstance(model, str):
            modeles_a_essayer = [model]
        elif model:
            modeles_a_essayer = list(model)
        else:
            modeles_a_essayer = [config.MODELE_FALLBACK]

        retries_max = config.RATE_LIMIT["retries_max"]
        backoff_initial = config.RATE_LIMIT["backoff_initial"]
        delais = [backoff_initial * (2**i) for i in range(retries_max)]

        debut = time.monotonic()
        meilleur = {"texte": "", "parsed": None, "tokens_entree": None, "tokens_sortie": None, "cout_estime": None}
        nb_retries_total = 0
        derniere_erreur = None
        quota_atteint = False

        for modele in modeles_a_essayer:
            for tentative in range(retries_max + 1):
                if tentative > 0:
                    delai = delais[tentative - 1]
                    logger.warning(
                        f"Nouvel essai ({tentative}/{retries_max}) pour {modele} "
                        f"dans {delai}s (raison précédente : {derniere_erreur})."
                    )
                    time.sleep(delai)
                    nb_retries_total += 1

                resultat = self._tenter(modele, system_prompt, user_prompt, temperature, max_tokens, json_attendu)
                self._mettre_a_jour_meilleur(meilleur, resultat)

                if resultat["succes"]:
                    _ecrire_cache(cle_cache, modele, resultat["texte"], resultat["tokens_entree"], resultat["tokens_sortie"], resultat["cout_estime"])
                    return self._finaliser_succes(resultat, modele, debut, nb_retries_total, modele != modeles_a_essayer[0])

                derniere_erreur = resultat["erreur"]
                if resultat.get("quota_atteint"):
                    quota_atteint = True
                    break
                if not resultat["retryable"]:
                    logger.warning(f"Erreur non retentable sur {modele} ({derniere_erreur}), passage au modèle suivant.")
                    break

            if quota_atteint:
                break

        # --- Repli sur l'auto-routeur (un seul essai), sauf si déjà essayé ou quota atteint ---
        modele_fallback_utilise = False
        if not quota_atteint and config.MODELE_FALLBACK not in modeles_a_essayer:
            logger.warning(
                f"Tous les modèles principaux ont échoué après {nb_retries_total} retries "
                f"(dernière erreur : {derniere_erreur}), repli sur {config.MODELE_FALLBACK}."
            )
            nb_retries_total += 1
            modele_fallback_utilise = True
            resultat = self._tenter(config.MODELE_FALLBACK, system_prompt, user_prompt, temperature, max_tokens, json_attendu)
            self._mettre_a_jour_meilleur(meilleur, resultat)

            if resultat["succes"]:
                _ecrire_cache(cle_cache, config.MODELE_FALLBACK, resultat["texte"], resultat["tokens_entree"], resultat["tokens_sortie"], resultat["cout_estime"])
                return self._finaliser_succes(resultat, config.MODELE_FALLBACK, debut, nb_retries_total, True)

            derniere_erreur = resultat["erreur"]

        # --- Échec complet : on retourne la meilleure réponse partielle obtenue ---
        duree = time.monotonic() - debut
        logger.error(
            f"Échec complet de l'appel LLM après {nb_retries_total} retries sur "
            f"{len(modeles_a_essayer)} modèle(s) (dernière erreur : {derniere_erreur})."
        )
        reponse = meilleur["parsed"] if json_attendu else meilleur["texte"]
        metadata = AppelMetadata(
            modele=modeles_a_essayer[0],
            succes=False,
            tokens_entree=meilleur["tokens_entree"],
            tokens_sortie=meilleur["tokens_sortie"],
            duree_secondes=duree,
            nb_retries=nb_retries_total,
            modele_fallback_utilise=modele_fallback_utilise,
            cout_estime=meilleur["cout_estime"],
            erreur=derniere_erreur,
        )
        return reponse, metadata

    @staticmethod
    def _mettre_a_jour_meilleur(meilleur: dict, resultat: dict) -> None:
        """Conserve la réponse la plus complète rencontrée (texte le plus long) parmi tous les essais."""
        if resultat["texte"] and len(resultat["texte"]) > len(meilleur["texte"]):
            meilleur["texte"] = resultat["texte"]
            meilleur["parsed"] = resultat["parsed"]
            meilleur["tokens_entree"] = resultat["tokens_entree"]
            meilleur["tokens_sortie"] = resultat["tokens_sortie"]
            meilleur["cout_estime"] = resultat["cout_estime"]

    @staticmethod
    def _finaliser_succes(resultat: dict, modele: str, debut: float, nb_retries: int, via_fallback: bool) -> tuple[Any, AppelMetadata]:
        duree = time.monotonic() - debut
        logger.info(
            f"Appel LLM réussi (modèle={modele}, tokens_in={resultat['tokens_entree']}, "
            f"tokens_out={resultat['tokens_sortie']}, durée={duree:.2f}s, retries={nb_retries}, "
            f"coût_estime={resultat['cout_estime']})."
        )
        metadata = AppelMetadata(
            modele=modele,
            succes=True,
            tokens_entree=resultat["tokens_entree"],
            tokens_sortie=resultat["tokens_sortie"],
            duree_secondes=duree,
            nb_retries=nb_retries,
            modele_fallback_utilise=via_fallback,
            cout_estime=resultat["cout_estime"],
        )
        return resultat["parsed"], metadata

    @classmethod
    def _respecter_pause_entre_appels(cls) -> None:
        """Impose un écart minimal entre deux appels réseau réels (RATE_LIMIT['pause_entre_appels'])."""
        if cls._dernier_appel_ts is not None:
            ecoule = time.monotonic() - cls._dernier_appel_ts
            attente = config.RATE_LIMIT["pause_entre_appels"] - ecoule
            if attente > 0:
                time.sleep(attente)
        cls._dernier_appel_ts = time.monotonic()

    def _tenter(
        self,
        modele: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        json_attendu: bool,
    ) -> dict:
        """
        Effectue une tentative unique d'appel HTTP à OpenRouter. Ne lève jamais
        d'exception : toute erreur est capturée et rapportée dans le dict
        retourné (clés 'erreur', 'retryable', 'quota_atteint') pour laisser
        l'appelant décider de retenter ou non.
        """
        resultat = {
            "succes": False, "retryable": True, "quota_atteint": False,
            "texte": "", "parsed": None,
            "tokens_entree": None, "tokens_sortie": None, "cout_estime": None,
            "erreur": None,
        }

        compteur_actuel = _lire_compteur_quotidien()
        max_requetes_jour = config.RATE_LIMIT["max_requetes_jour"]
        if compteur_actuel >= max_requetes_jour:
            resultat["erreur"] = f"limite quotidienne de requêtes OpenRouter atteinte ({compteur_actuel}/{max_requetes_jour})"
            resultat["retryable"] = False
            resultat["quota_atteint"] = True
            logger.error(f"Appel bloqué : {resultat['erreur']}.")
            return resultat

        self._respecter_pause_entre_appels()
        _incrementer_compteur_quotidien()

        try:
            reponse_http = requests.post(
                f"{self.base_url}{ENDPOINT_CHAT_COMPLETIONS}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": modele,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    # Demande à OpenRouter d'inclure le coût de la génération dans la réponse
                    "usage": {"include": True},
                },
                timeout=90,
            )
        except requests.RequestException as erreur:
            resultat["erreur"] = f"erreur réseau : {erreur}"
            return resultat

        if reponse_http.status_code in CODES_HTTP_RETRYABLE or reponse_http.status_code >= 500:
            resultat["erreur"] = f"HTTP {reponse_http.status_code} : {reponse_http.text[:200]}"
            return resultat

        if reponse_http.status_code != 200:
            resultat["erreur"] = f"HTTP {reponse_http.status_code} (non retentable) : {reponse_http.text[:200]}"
            resultat["retryable"] = False
            return resultat

        try:
            corps = reponse_http.json()
            texte = corps["choices"][0]["message"]["content"]
            usage = corps.get("usage") or {}
        except (ValueError, KeyError, IndexError) as erreur:
            resultat["erreur"] = f"réponse HTTP 200 mais corps inattendu : {erreur}"
            return resultat

        resultat["texte"] = texte
        resultat["tokens_entree"] = usage.get("prompt_tokens")
        resultat["tokens_sortie"] = usage.get("completion_tokens")
        resultat["cout_estime"] = usage.get("cost")

        if json_attendu:
            parsed = extraire_json(texte)
            if parsed is None:
                resultat["erreur"] = "réponse JSON invalide ou non extractible"
                return resultat
            resultat["parsed"] = parsed
        else:
            resultat["parsed"] = texte

        resultat["succes"] = True
        return resultat