# Règles de validation de structure

Ce document liste les vérifications effectuées par `structure/agent_structure.py`,
le premier détecteur du pipeline d'audit. Il juge uniquement la **forme** d'un
fichier (colonnes, types, doublons) — jamais la plausibilité de ses valeurs
métier (rôle de `stats/` et des agents LLM, plus tard dans le pipeline).

Toutes les anomalies produites ont `type: "structure"` et `source: "stats"`
(détection déterministe, sans LLM). Convention sur le champ `ligne` : `0`
signifie que l'anomalie concerne tout le fichier ou une colonne entière, pas
une ligne précise. Seule `verifier_lignes_dupliquees` renseigne un vrai numéro
de ligne (index pandas 0-based de la première ligne dupliquée trouvée).

## `verifier_fichier_vide`

- **Sévérité** : `critical`
- **Pourquoi** : un fichier sans ligne ou sans colonne ne peut faire l'objet
  d'aucune analyse ultérieure. C'est la seule vérification qui court-circuite
  les autres (`analyser()` s'arrête immédiatement si elle déclenche).

## `verifier_colonnes_sans_nom`

- **Sévérité** : `high` si la colonne contient des données, `low` si elle est
  entièrement vide.
- **Pourquoi** : une colonne sans nom (`Unnamed: N`, artefact pandas/Excel, ou
  nom vide/blanc) signale généralement un décalage de colonnes à l'export ou
  un en-tête mal renseigné. Si elle ne contient aucune donnée, c'est un simple
  artefact d'export sans impact (sévérité réduite).

## `verifier_colonnes_dupliquees`

- **Sévérité** : `high`
- **Pourquoi** : deux colonnes portant le même nom créent une ambiguïté à la
  lecture (laquelle utiliser ?) et peuvent masquer une perte de données selon
  l'outil qui lit le fichier.

## `verifier_colonnes_vides`

- **Sévérité** : `medium`
- **Pourquoi** : une colonne à 100% de valeurs manquantes n'apporte aucune
  information exploitable. Sévérité modérée : ce n'est pas bloquant (le reste
  du fichier reste utilisable), mais mérite d'être signalé au fournisseur.
- Exclut les colonnes déjà signalées par `verifier_colonnes_sans_nom` pour ne
  pas compter deux fois la même anomalie.

## `verifier_lignes_dupliquees`

- **Sévérité** : `medium`
- **Pourquoi** : des lignes strictement identiques suggèrent un doublon
  d'extraction (même véhicule/contrat scrapé plusieurs fois). Une seule
  anomalie agrégée est produite (nombre de doublons + aperçu des index),
  plutôt qu'une anomalie par ligne, pour ne pas noyer le rapport.

## `verifier_taux_valeurs_manquantes`

- **Sévérité** : `medium` si le taux de valeurs manquantes est ≥ 50%,
  `high` si ≥ 90%.
- **Pourquoi** : une colonne majoritairement vide est peu fiable pour toute
  analyse statistique ou métier ultérieure. Les seuils sont des constantes
  (`SEUIL_NAN_MEDIUM`, `SEUIL_NAN_HIGH`) en haut du fichier, ajustables sans
  toucher à la logique.
- Exclut les colonnes sans nom et les colonnes à 100% de NaN, déjà couvertes
  par les deux vérifications précédentes.

## `verifier_types_incoherents`

- **Sévérité** : `medium`
- **Pourquoi** : une colonne texte qui mélange des valeurs numériques (ou
  dates) et du texte non convertible indique un problème de saisie ou
  d'export (ex. "1200" et "sur devis" dans la même colonne "Prix").
- **Heuristique** : sur un échantillon de valeurs non nulles (constante
  `TAILLE_ECHANTILLON_TYPES`), on calcule le taux de valeurs convertibles en
  nombre (formats US et européens : virgule décimale, espace comme séparateur
  de milliers) et en date (jour en premier). Seul un taux **intermédiaire**
  (entre `SEUIL_MELANGE_TYPE_BAS` = 60% et `SEUIL_MELANGE_TYPE_HAUT` = 95%)
  déclenche l'anomalie : en dessous, c'est une colonne texte normale ; au-delà,
  c'est une colonne homogène (y compris au format européen), pas une anomalie.
- **Faux positifs évités** : les formats européens ("1 234,56", "31/12/2025")
  ne sont jamais signalés tant qu'ils sont majoritaires dans la colonne.
