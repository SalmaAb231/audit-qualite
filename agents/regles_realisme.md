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

**Échantillonnage des fichiers géants**, règle alignée sur
`agents/agent_coherence.py` (ajouté après coup, pour que les deux agents LLM
se comportent de façon cohérente sur les plus gros fichiers) : un fichier de
plus de `config.SEUIL_FICHIER_GEANT` lignes est échantillonné à
`config.MAX_LIGNES_PAR_FICHIER` lignes avant découpage, avec une graine fixe
(`SEED_ECHANTILLONNAGE = 42`, locale à ce module) pour que deux analyses du
même fichier envoient les mêmes lignes. En dessous de ce seuil, **aucune
troncature** : le fichier est traité intégralement, même s'il dépasse
`MAX_LIGNES_PAR_FICHIER`. Toujours journalisé (`logger.warning`), jamais
silencieux.

Ce mécanisme est désactivé explicitement dans le pipeline orchestré
(`echantillonner=False`) : `orchestrateur.py` a déjà choisi les lignes
suspectes et aucune ne doit disparaître dans un second tirage. Il reste un
filet de sécurité pour l'appel direct de ce module (CLI, tests) sur un fichier
complet non filtré.

## Robustesse

- Un échec d'appel LLM sur un chunk (exception, timeout, quota ou JSON invalide
  après tous les retries) n'interrompt pas les chunks suivants, mais il n'est
  plus assimilé à « aucune anomalie ». `analyser_avec_statut()` le compte dans
  `chunks_failed`/`nb_lignes_echec` et remonte son message dans `error`.
- `SUCCESS` signifie que tous les chunks ont réussi, `PARTIAL` qu'une partie
  seulement a réussi, et `FAILED` qu'aucun chunk n'a réussi. Les anomalies des
  chunks réussis sont conservées même lorsque le statut global est `PARTIAL`.
- Un DataFrame vide retourne un résultat `SUCCESS` avec zéro chunk et zéro
  ligne, sans consommer de requête.
- Chaque anomalie brute renvoyée par le modèle passe par
  `parse_utils.valider_anomalies` (champs requis, énumérations, types) avant
  d'être acceptée.

Deux interfaces sont maintenues :

- `analyser_avec_statut(...)` retourne le résultat structuré complet et doit
  être utilisé par l'orchestrateur ;
- `analyser(...)` conserve la compatibilité historique et retourne uniquement
  la liste d'anomalies. Il ne faut pas l'utiliser lorsqu'un appelant doit
  distinguer une analyse propre d'un échec technique.

## Vérification web (pas de tool calling)

`config.py` prévoyait à l'origine `gpt-oss-120b:free` en premier choix pour
son support du tool calling — mais ce modèle a disparu du catalogue gratuit
d'OpenRouter, et le tool calling reste globalement peu fiable sur les
modèles `:free` (voir l'historique de ce module). Plutôt qu'un vrai
tool-calling agentique (le modèle décide lui-même quand chercher, nécessite
de modifier `agents/llm_client.py`, socle partagé avec Cohérence), la
vérification web est un **second passage fixe, après coup** :

1. Le modèle juge d'abord comme avant, sans accès web (`_analyser_chunk`).
2. Pour chaque anomalie candidate, une recherche DuckDuckGo
   (`_rechercher_web`, bibliothèque `ddgs`, gratuite, sans clé API) — requête
   construite à partir des champs `valeur`/`colonnes` déjà produits par le
   modèle (`_construire_requete_recherche`), schema-agnostic.
3. **Un seul appel LLM supplémentaire pour tout le chunk** (pas un par
   anomalie, `_verifier_avec_recherche_web`) : on montre au modèle toutes
   les anomalies candidates avec leurs résultats de recherche, il confirme
   ou retire celles que le web contredit clairement. Par défaut, une
   anomalie est conservée (biais volontaire vers "garder" : des résultats
   vides ou hors sujet ne suffisent pas à la retirer).

**Découverte motivant cet ajout** : en test réel sur `NL_B2B_VLV_OFF.xlsx`,
Réalisme a affirmé avec la sévérité `critical` que "Volvo ne commercialise
aucun modèle nommé EX40" — alors qu'une simple recherche web montre des
articles réels sur le "2026 Volvo EX40". Connaissances du modèle figées à
son entraînement, présentées avec une confiance trompeuse.

**Défensif à chaque étape**, contrôlable par `config.RECHERCHE_WEB_ACTIVEE` :
une recherche qui échoue (réseau, blocage DuckDuckGo, aucun résultat) ou un
appel de vérification qui échoue (erreur, réponse invalide) conserve les
anomalies candidates **telles quelles**, sans jamais bloquer le pipeline ni
perdre du signal — la vérification est un filtre en plus, jamais un
point de défaillance supplémentaire.

**Limites connues** :
- `ddgs` interroge DuckDuckGo sans API officielle : peut se dégrader ou se
  bloquer sans préavis, sans garantie de service.
- La requête de recherche est construite uniquement à partir de `valeur` et
  `colonnes` (déjà produits par le modèle), pas du contexte complet de la
  ligne : parfois peu précise (ex. juste "EX40 Model" plutôt que "Volvo
  EX40 2026 modèle électrique").
- Coût : un appel LLM de vérification de plus par chunk contenant au moins
  une anomalie candidate — à surveiller sur le quota partagé
  (`config.RATE_LIMIT["max_requetes_jour"]`).

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
