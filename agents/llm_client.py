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
from pathlib import Path
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

# Classification stable utilisée par la politique FREE tier.
ERREUR_UPSTREAM_RATE_LIMIT = "upstream_rate_limited"
ERREUR_QUOTA_EPUISE = "quota_exhausted"
ERREUR_SERVEUR = "server_error"
ERREUR_RESEAU = "network_error"
ERREUR_REPONSE_INVALIDE = "invalid_openrouter_response"
ERREUR_JSON_INVALIDE = "invalid_business_json"
ERREUR_PERMANENTE = "permanent_client_error"


def _classifier_http_429(message: str) -> str:
    texte = str(message or "").lower()
    marqueurs_quota = (
        "quota exceeded",
        "quota exhausted",
        "daily limit",
        "free-models-per-day",
        "requests per day",
        "per-day limit",
        "insufficient credits",
        "credit limit",
    )
    if any(marqueur in texte for marqueur in marqueurs_quota):
        return ERREUR_QUOTA_EPUISE
    return ERREUR_UPSTREAM_RATE_LIMIT


def _retries_autorises(error_type: str | None) -> int:
    if error_type in {
        ERREUR_SERVEUR,
        ERREUR_RESEAU,
        ERREUR_REPONSE_INVALIDE,
        ERREUR_JSON_INVALIDE,
    }:
        return 1
    return 0


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
    error_type: str | None = None
    nb_tentatives: int = 0
    budget_atteint: bool = False


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
# Compteur quotidien de requêtes réelles, isolé par clé OpenRouter sous
# config.QUOTA_LLM_DIR, pour ne jamais dépasser la limite locale par clé.
# ---------------------------------------------------------------------------


def _fingerprint_api_key(api_key: str) -> str:
    """Retourne les 12 premiers hexadécimaux du SHA-256 d'une clé non vide."""
    if not api_key:
        raise ValueError("Une clé OpenRouter non vide est requise pour calculer son fingerprint.")
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _api_key_effective(api_key: str | None = None) -> str:
    """Résout la clé explicite ou, sans argument, celle de la configuration."""
    return config.OPENROUTER_API_KEY if api_key is None else api_key


def _chemin_compteur_quotidien(api_key: str | None = None) -> Path | None:
    """Construit le chemin du compteur sans jamais incorporer la clé en clair."""
    cle_effective = _api_key_effective(api_key)
    if not cle_effective:
        return None
    fingerprint = _fingerprint_api_key(cle_effective)
    return config.QUOTA_LLM_DIR / f"compteur_{fingerprint}.json"


def _lire_compteur_quotidien(api_key: str | None = None) -> int:
    """Lit le compteur du jour pour la clé concernée, ou 0 s'il est inutilisable."""
    chemin = _chemin_compteur_quotidien(api_key)
    if chemin is None:
        return 0
    try:
        donnees = json.loads(chemin.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return 0
    if not isinstance(donnees, dict) or donnees.get("date") != date.today().isoformat():
        return 0
    compteur = donnees.get("compteur", 0)
    if isinstance(compteur, bool) or not isinstance(compteur, int):
        return 0
    return max(0, compteur)


def _incrementer_compteur_quotidien(api_key: str | None = None) -> int:
    """Incrémente le compteur de la clé après décision d'effectuer un appel réseau."""
    cle_effective = _api_key_effective(api_key)
    chemin = _chemin_compteur_quotidien(cle_effective)
    if chemin is None:
        raise ValueError("Impossible d'incrémenter le quota OpenRouter sans clé API.")
    compteur = _lire_compteur_quotidien(cle_effective) + 1
    config.QUOTA_LLM_DIR.mkdir(parents=True, exist_ok=True)
    chemin.write_text(
        json.dumps({"date": date.today().isoformat(), "compteur": compteur}),
        encoding="utf-8",
    )
    logger.info(
        f"Quota OpenRouter local pour fingerprint {_fingerprint_api_key(cle_effective)} : "
        f"{compteur}/{config.RATE_LIMIT['max_requetes_jour']} requêtes utilisées."
    )
    return compteur


def quota_restant(api_key: str | None = None) -> int:
    """Quota local restant pour la clé explicite, ou celle de config par défaut."""
    cle_effective = _api_key_effective(api_key)
    maximum = config.RATE_LIMIT["max_requetes_jour"]
    if not cle_effective:
        logger.warning(
            "Quota OpenRouter local non associé : aucune clé API configurée. "
            "Le quota théorique est retourné sans créer de fichier compteur."
        )
        return maximum
    utilise = _lire_compteur_quotidien(cle_effective)
    restant = max(0, maximum - utilise)
    logger.debug(
        f"Quota OpenRouter local pour fingerprint {_fingerprint_api_key(cle_effective)} : "
        f"{utilise}/{maximum} requêtes utilisées."
    )
    return restant


class LLMClient:
    """
    Client d'appel à l'API OpenRouter, avec cascade de modèles, retries
    progressifs, cache de réponses et respect des limites du tier gratuit.
    """

    # Horodatage (time.monotonic) du dernier appel réseau réel, partagé par
    # toutes les instances du processus, pour respecter RATE_LIMIT["pause_entre_appels"]
    # même si plusieurs LLMClient() sont créés (un par agent, par exemple).
    _dernier_appel_ts: float | None = None
    _echecs_upstream: dict[tuple[str, str], int] = {}
    _circuits_ouverts_jusqua: dict[tuple[str, str], float] = {}

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.base_url = (base_url or config.OPENROUTER_BASE_URL).rstrip("/")
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY est vide : les appels LLM échoueront.")

    def _circuit_key(self, modele: str) -> tuple[str, str] | None:
        if not self.api_key:
            return None
        return (_fingerprint_api_key(self.api_key), modele)

    def _circuit_ouvert(self, modele: str) -> bool:
        cle = self._circuit_key(modele)
        if cle is None:
            return False
        expiration = self._circuits_ouverts_jusqua.get(cle)
        if expiration is None:
            return False
        if time.monotonic() >= expiration:
            self._circuits_ouverts_jusqua.pop(cle, None)
            self._echecs_upstream.pop(cle, None)
            return False
        return True

    def _enregistrer_echec_upstream(self, modele: str) -> None:
        cle = self._circuit_key(modele)
        if cle is None:
            return
        nombre = self._echecs_upstream.get(cle, 0) + 1
        self._echecs_upstream[cle] = nombre
        seuil = config.OPENROUTER_CIRCUIT_BREAKER_THRESHOLD
        if nombre >= seuil:
            self._circuits_ouverts_jusqua[cle] = (
                time.monotonic() + config.OPENROUTER_CIRCUIT_BREAKER_COOLDOWN_SECONDS
            )
            logger.warning(
                f"Circuit OpenRouter ouvert pour modèle={modele}, fingerprint={cle[0]} "
                f"après {nombre} réponses upstream rate-limited."
            )

    def _reinitialiser_circuit(self, modele: str) -> None:
        cle = self._circuit_key(modele)
        if cle is not None:
            self._echecs_upstream.pop(cle, None)
            self._circuits_ouverts_jusqua.pop(cle, None)

    def appeler(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | list[str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_attendu: bool = True,
        attempt_budget: int | None = None,
    ) -> tuple[Any, AppelMetadata]:
        """
        Envoie un prompt système + utilisateur à une cascade de modèles OpenRouter.

        `model` peut être un identifiant unique, ou (cas normal avec
        config.MODELES) une liste ordonnée. Retries et changements de modèle
        partagent le plafond global config.MAX_LLM_ATTEMPTS_PER_CHUNK. Les
        erreurs transitoires autorisent au plus un retry court par modèle;
        les erreurs permanentes passent immédiatement au modèle suivant. Le
        fallback OpenRouter est soumis au même plafond, jamais ajouté après.

        Si un appel strictement identique (mêmes prompts/température/
        max_tokens) a déjà réussi, la réponse est servie depuis
        config.CACHE_LLM_DIR sans consommer de requête. Sinon, chaque
          tentative réseau réelle est comptée dans le fichier quota de la clé
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

        # Le fallback fait partie du même budget global : il n'ajoute jamais
        # une tentative au-delà du plafond du chunk.
        if config.MODELE_FALLBACK not in modeles_a_essayer:
            modeles_a_essayer.append(config.MODELE_FALLBACK)
        modeles_a_essayer = list(dict.fromkeys(modeles_a_essayer))

        budget_configure = max(1, int(config.MAX_LLM_ATTEMPTS_PER_CHUNK))
        budget = (
            budget_configure
            if attempt_budget is None
            else min(budget_configure, max(0, int(attempt_budget)))
        )
        if budget == 0:
            return None, AppelMetadata(
                modele=modeles_a_essayer[0],
                succes=False,
                erreur="Budget global d'appels LLM épuisé pour ce chunk.",
                error_type=ERREUR_QUOTA_EPUISE,
                nb_tentatives=0,
                budget_atteint=True,
            )
        backoff = max(0.0, float(config.FREE_TIER_RETRY_BACKOFF_SECONDS))
        debut = time.monotonic()
        meilleur = {
            "texte": "",
            "parsed": None,
            "tokens_entree": None,
            "tokens_sortie": None,
            "cout_estime": None,
        }
        nb_tentatives = 0
        nb_retries_total = 0
        derniere_erreur = None
        dernier_type_erreur = None
        quota_atteint = False
        modele_fallback_utilise = False

        for modele in modeles_a_essayer:
            if nb_tentatives >= budget or quota_atteint:
                break
            if self._circuit_ouvert(modele):
                logger.warning(f"Modèle {modele} ignoré : circuit upstream temporairement ouvert.")
                continue
            if modele == config.MODELE_FALLBACK:
                modele_fallback_utilise = True

            retries_effectues_modele = 0
            while nb_tentatives < budget:
                resultat = self._tenter(
                    modele,
                    system_prompt,
                    user_prompt,
                    temperature,
                    max_tokens,
                    json_attendu,
                )
                nb_tentatives += 1
                self._mettre_a_jour_meilleur(meilleur, resultat)

                if resultat["succes"]:
                    self._reinitialiser_circuit(modele)
                    _ecrire_cache(
                        cle_cache,
                        modele,
                        resultat["texte"],
                        resultat["tokens_entree"],
                        resultat["tokens_sortie"],
                        resultat["cout_estime"],
                    )
                    return self._finaliser_succes(
                        resultat,
                        modele,
                        debut,
                        nb_retries_total,
                        modele == config.MODELE_FALLBACK,
                        nb_tentatives,
                    )

                derniere_erreur = resultat["erreur"]
                dernier_type_erreur = resultat.get("error_type")
                if dernier_type_erreur == ERREUR_UPSTREAM_RATE_LIMIT:
                    self._enregistrer_echec_upstream(modele)
                if resultat.get("quota_atteint"):
                    quota_atteint = True
                    break

                maximum_retries = _retries_autorises(dernier_type_erreur)
                if retries_effectues_modele >= maximum_retries or self._circuit_ouvert(modele):
                    logger.warning(
                        f"Passage au modèle suivant après erreur {dernier_type_erreur} "
                        f"sur {modele} ({derniere_erreur})."
                    )
                    break

                retries_effectues_modele += 1
                nb_retries_total += 1
                if backoff:
                    time.sleep(backoff)

        if nb_tentatives == 0 and not quota_atteint:
            derniere_erreur = "tous les circuits des modèles candidats sont temporairement ouverts"
            dernier_type_erreur = ERREUR_UPSTREAM_RATE_LIMIT
        budget_atteint = nb_tentatives >= budget and not quota_atteint
        if budget_atteint:
            derniere_erreur = (
                f"budget global de {budget} tentative(s) LLM atteint pour ce chunk; "
                f"dernière erreur: {derniere_erreur or 'indisponible'}"
            )

        duree = time.monotonic() - debut
        logger.error(
            f"Échec complet de l'appel LLM après {nb_tentatives}/{budget} tentative(s) "
            f"réseau (dernière erreur : {derniere_erreur})."
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
            error_type=dernier_type_erreur,
            nb_tentatives=nb_tentatives,
            budget_atteint=budget_atteint,
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
    def _finaliser_succes(
        resultat: dict,
        modele: str,
        debut: float,
        nb_retries: int,
        via_fallback: bool,
        nb_tentatives: int,
    ) -> tuple[Any, AppelMetadata]:
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
            nb_tentatives=nb_tentatives,
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
            "erreur": None, "error_type": None,
        }

        if not self.api_key:
            resultat["erreur"] = "OPENROUTER_API_KEY absente"
            resultat["error_type"] = ERREUR_PERMANENTE
            resultat["retryable"] = False
            logger.error("Appel OpenRouter bloqué : aucune clé API configurée.")
            return resultat

        fingerprint = _fingerprint_api_key(self.api_key)
        compteur_actuel = _lire_compteur_quotidien(self.api_key)
        max_requetes_jour = config.RATE_LIMIT["max_requetes_jour"]
        if compteur_actuel >= max_requetes_jour:
            resultat["erreur"] = f"limite quotidienne de requêtes OpenRouter atteinte ({compteur_actuel}/{max_requetes_jour})"
            resultat["retryable"] = False
            resultat["quota_atteint"] = True
            resultat["error_type"] = ERREUR_QUOTA_EPUISE
            logger.error(
                f"Appel bloqué pour fingerprint {fingerprint} : {resultat['erreur']}."
            )
            return resultat

        self._respecter_pause_entre_appels()
        _incrementer_compteur_quotidien(self.api_key)

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
            resultat["error_type"] = ERREUR_RESEAU
            return resultat

        if reponse_http.status_code == 429:
            resultat["erreur"] = f"HTTP {reponse_http.status_code} : {reponse_http.text[:200]}"
            resultat["error_type"] = _classifier_http_429(reponse_http.text)
            if resultat["error_type"] == ERREUR_QUOTA_EPUISE:
                resultat["quota_atteint"] = True
                resultat["retryable"] = False
            return resultat

        if reponse_http.status_code >= 500:
            resultat["erreur"] = f"HTTP {reponse_http.status_code} : {reponse_http.text[:200]}"
            resultat["error_type"] = ERREUR_SERVEUR
            return resultat

        if reponse_http.status_code != 200:
            resultat["erreur"] = f"HTTP {reponse_http.status_code} (non retentable) : {reponse_http.text[:200]}"
            resultat["error_type"] = ERREUR_PERMANENTE
            resultat["retryable"] = False
            return resultat

        try:
            corps = reponse_http.json()
            texte = corps["choices"][0]["message"]["content"]
            usage = corps.get("usage") or {}
        except (ValueError, KeyError, IndexError) as erreur:
            resultat["erreur"] = f"réponse HTTP 200 mais corps inattendu : {erreur}"
            resultat["error_type"] = ERREUR_REPONSE_INVALIDE
            return resultat

        resultat["texte"] = texte
        resultat["tokens_entree"] = usage.get("prompt_tokens")
        resultat["tokens_sortie"] = usage.get("completion_tokens")
        resultat["cout_estime"] = usage.get("cost")

        if json_attendu:
            parsed = extraire_json(texte)
            if parsed is None:
                resultat["erreur"] = "réponse JSON invalide ou non extractible"
                resultat["error_type"] = ERREUR_JSON_INVALIDE
                return resultat
            resultat["parsed"] = parsed
        else:
            resultat["parsed"] = texte

        resultat["succes"] = True
        return resultat
