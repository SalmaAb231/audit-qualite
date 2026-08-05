"""
Client HTTP pour l'API OpenRouter, socle commun à tous les agents LLM du
pipeline (Cohérence, Réalisme, Judge, Reporting).

Centralise l'appel réseau, les retries progressifs, le fallback de modèle et
la journalisation, afin qu'un agent n'ait qu'à fournir ses prompts et à
récupérer une réponse déjà robustifiée, sans dupliquer cette logique.
"""

import time
from dataclasses import dataclass
from typing import Any

import requests
from loguru import logger

import config
from agents.parse_utils import extraire_json

# Délais progressifs (en secondes) entre chaque nouvelle tentative sur le
# modèle principal. 3 délais -> 3 retries -> 4 essais au total avant fallback.
DELAIS_RETRY = (2, 8, 20)

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
    erreur: str | None = None


class LLMClient:
    """Client d'appel à l'API OpenRouter, avec retries progressifs et fallback de modèle."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self.base_url = (base_url or config.OPENROUTER_BASE_URL).rstrip("/")
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY est vide : les appels LLM échoueront.")

    def appeler(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_attendu: bool = True,
    ) -> tuple[Any, AppelMetadata]:
        """
        Envoie un prompt système + utilisateur à un modèle OpenRouter.

        Essaie `model` (ou config.MODELE_FALLBACK si non précisé) avec jusqu'à
        len(DELAIS_RETRY) retries progressifs sur erreur réseau, HTTP 429/5xx,
        ou JSON invalide (si json_attendu). Une erreur HTTP non transitoire
        (4xx hors 429) interrompt immédiatement les essais sur ce modèle.
        Si le modèle principal échoue complètement, retente une dernière fois
        avec config.MODELE_FALLBACK.

        Retourne (reponse_parsee, metadata). Si json_attendu, reponse_parsee
        est le JSON extrait (dict/list) en cas de succès, ou la meilleure
        réponse partielle récupérée sinon (peut être None). Si json_attendu
        est faux, reponse_parsee est le texte brut de la réponse.
        """
        modele_principal = model or config.MODELE_FALLBACK
        temperature = TEMPERATURE_PAR_DEFAUT if temperature is None else temperature
        max_tokens = MAX_TOKENS_PAR_DEFAUT if max_tokens is None else max_tokens

        debut = time.monotonic()
        meilleur = {"texte": "", "parsed": None, "tokens_entree": None, "tokens_sortie": None, "cout_estime": None}
        nb_retries = 0
        derniere_erreur = None

        # --- Essais sur le modèle principal ---
        for tentative in range(len(DELAIS_RETRY) + 1):
            if tentative > 0:
                delai = DELAIS_RETRY[tentative - 1]
                logger.warning(
                    f"Nouvel essai ({tentative}/{len(DELAIS_RETRY)}) pour {modele_principal} "
                    f"dans {delai}s (raison précédente : {derniere_erreur})."
                )
                time.sleep(delai)
                nb_retries += 1

            resultat = self._tenter(modele_principal, system_prompt, user_prompt, temperature, max_tokens, json_attendu)
            self._mettre_a_jour_meilleur(meilleur, resultat)

            if resultat["succes"]:
                return self._finaliser_succes(resultat, modele_principal, debut, nb_retries, False)

            derniere_erreur = resultat["erreur"]
            if not resultat["retryable"]:
                logger.warning(f"Erreur non retentable sur {modele_principal} ({derniere_erreur}), arrêt des essais sur ce modèle.")
                break

        # --- Repli sur le modèle de secours (un seul essai) ---
        modele_fallback_utilise = False
        if modele_principal != config.MODELE_FALLBACK:
            logger.warning(
                f"Modèle {modele_principal} en échec après {nb_retries} retries "
                f"(erreur : {derniere_erreur}), repli sur {config.MODELE_FALLBACK}."
            )
            nb_retries += 1
            modele_fallback_utilise = True
            resultat = self._tenter(config.MODELE_FALLBACK, system_prompt, user_prompt, temperature, max_tokens, json_attendu)
            self._mettre_a_jour_meilleur(meilleur, resultat)

            if resultat["succes"]:
                return self._finaliser_succes(resultat, config.MODELE_FALLBACK, debut, nb_retries, True)

            derniere_erreur = resultat["erreur"]

        # --- Échec complet : on retourne la meilleure réponse partielle obtenue ---
        duree = time.monotonic() - debut
        logger.error(
            f"Échec complet de l'appel LLM après {nb_retries} retries "
            f"(modèle principal={modele_principal}, dernière erreur : {derniere_erreur})."
        )
        reponse = meilleur["parsed"] if json_attendu else meilleur["texte"]
        metadata = AppelMetadata(
            modele=modele_principal,
            succes=False,
            tokens_entree=meilleur["tokens_entree"],
            tokens_sortie=meilleur["tokens_sortie"],
            duree_secondes=duree,
            nb_retries=nb_retries,
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
        retourné (clés 'erreur' et 'retryable') pour laisser l'appelant décider
        de retenter ou non.
        """
        resultat = {
            "succes": False, "retryable": True, "texte": "", "parsed": None,
            "tokens_entree": None, "tokens_sortie": None, "cout_estime": None,
            "erreur": None,
        }

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
