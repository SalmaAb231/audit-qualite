# Règles de l'agent Réalisme

Ce document décrit `agents/agent_realisme.py`, le troisième détecteur du
pipeline (après `structure/` et `stats/`), premier détecteur basé sur un
LLM. Il juge le **réalisme individuel des valeurs**, au regard des
connaissances générales du modèle sur le marché automobile réel — jamais la
forme du fichier (rôle de `structure/`), jamais la plausibilité purement
statistique (rôle de `stats/`).

Toutes les anomalies produites ont `type: "realisme"` et `source: "llm"` :
ces deux champs sont réécrits par le code après la réponse du modèle, jamais
laissés tels que renvoyés — un LLM qui se tromperait de type ou de source ne
peut pas casser le contrat.

## Frontière avec l'agent Cohérence

Les deux agents LLM se ressemblent mais répondent à des questions
différentes, pour éviter le double signalement d'une même ligne par les deux :

- **Cohérence** : la ligne est-elle logiquement contradictoire avec
  elle-même (plusieurs champs qui se contredisent, ex. `date_fin` avant
  `date_debut`) ?
- **Réalisme** (ce module) : une valeur, prise isolément ou en combinaison
  avec 1-2 autres (ex. marque + puissance), correspond-elle à quelque chose
  qui existe réellement sur le marché automobile ?

Le prompt système liste explicitement des exemples des deux catégories pour
que le modèle sache où s'arrêter (ex. une Fiat Panda à 900 ch est du
réalisme ; une date de fin avant une date de début est de la cohérence, pas
de son ressort).

## Convention 'ligne' et découpage en chunks

Le fichier est découpé en tranches de `config.CHUNK_SIZE` lignes (constante
commune aux agents Cohérence et Réalisme, définie dans `config.py`). Chaque
tranche est envoyée au modèle sous forme de tableau markdown avec une
colonne explicite `ligne` = index pandas d'origine (0-based, même convention
que `structure/` et `stats/`) : le modèle doit la recopier telle quelle dans
le champ `ligne` de chaque anomalie plutôt que de recalculer une position
locale au chunk. Aucune vérification automatique ne recoupe cette valeur
avec l'index réel : un modèle qui se trompe de ligne produira une anomalie
mal localisée sans que le code puisse le détecter (limite connue).

`config.MAX_LIGNES_PAR_FICHIER` (0 = désactivé) tronque le fichier avant
découpage, pour limiter la consommation de requêtes pendant le
développement — cette troncature est journalisée (`logger.warning`), jamais
silencieuse.

## Robustesse

- Un échec d'appel LLM sur un chunk (exception, timeout, JSON invalide même
  après les stratégies de récupération de `parse_utils.extraire_json`) est
  journalisé et traité comme "aucune anomalie sur ce chunk" : il n'interrompt
  jamais le traitement des chunks suivants ni ne fait échouer `analyser()`.
- Un DataFrame vide retourne `[]` immédiatement, sans consommer de requête.
- Chaque anomalie brute renvoyée par le modèle passe par
  `parse_utils.valider_anomalies` (champs requis, énumérations, types) avant
  d'être acceptée.

## Limite connue : pas de recherche web

`config.py` prévoit `gpt-oss-120b:free` en premier choix pour cet agent
précisément pour son support du tool calling (recherche web pour vérifier
des faits, ex. "cette combinaison marque/modèle/puissance existe-t-elle
vraiment ?"). Cette version s'appuie uniquement sur les connaissances
internes du modèle, sans aucun appel d'outil : `agents/llm_client.py` (socle
partagé avec Cohérence) n'implémente pas encore l'envoi du paramètre
`tools` à l'API OpenRouter. Conséquence : les jugements de réalisme peuvent
être moins fiables sur des véhicules récents ou peu courants (connaissances
figées à l'entraînement du modèle), sans garde-fou pour le détecter. Ajouter
le tool calling est une extension future du socle partagé, pas de ce module
seul.

## Limite découverte en testant sur données réelles : le modèle ne connaît pas la date du jour

Premier test réel (`NL_B2B_VLV_OFF.xlsx`, 20 lignes, 1 chunk) : les 17
anomalies retournées disaient toutes la même chose — une colonne
`Current_time` valant début mars 2026 jugée "dans le futur", alors que le
fichier a été testé en août 2026 (donc une date bien passée). Le modèle n'a
aucune horloge interne fiable : il juge le futur/passé par rapport à une
estimation de sa propre date d'entraînement, pas par rapport à la vraie date
du jour. Corrigé en injectant explicitement `date.today()` dans le prompt
système (`_construire_prompt_systeme()`), recalculée à chaque appel plutôt
que figée à l'import du module.

Deux autres constats du même test, hors périmètre de ce module (pas
corrigés ici) :
- `openai/gpt-oss-120b:free` (premier modèle de `config.MODELES["realisme"]`)
  a répondu HTTP 404 — retiré du catalogue gratuit d'OpenRouter (le
  catalogue `:free` tourne sans préavis, voir l'avertissement dans
  `config.py`). Le fallback `nvidia/nemotron-3-ultra-550b-a55b:free` a pris
  le relais mais a mis ~170s et consommé 3 requêtes de quota pour un seul
  petit chunk (404 + 1 échec réseau + 1 retry réussi).
- La réponse a atteint exactement `MAX_TOKENS["realisme"]` (4096/4096) alors
  que le chunk ne faisait que 20 lignes, en grande partie parce que le
  modèle a répété une justification quasi identique 17 fois plutôt que de
  regrouper les lignes concernées en une seule anomalie de portée colonne
  (convention déjà utilisée dans `structure/` et `stats/` pour ce genre de
  cas). Risque réel de troncature sur des chunks de `config.CHUNK_SIZE`
  lignes (500) si ce module continue à produire une anomalie par ligne pour
  un même défaut systémique — pas encore traité, décision à prendre avec
  l'équipe (dédupliquer côté prompt, côté post-traitement, ou accepter le
  risque).

## Tests

`tests/test_agent_realisme.py` n'effectue **aucun appel réseau réel** : un
client LLM factice (`_ClientFactice`) simule les réponses de
`LLMClient.appeler()`. Objectif : ne jamais consommer de requêtes du quota
gratuit (`config.RATE_LIMIT["max_requetes_jour"] = 45`, partagé par tout le
projet) pendant les tests automatisés, exécutables à volonté en CI comme en
local.
