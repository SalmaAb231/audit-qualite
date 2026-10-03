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

- **Sévérité** : `high` si la colonne contient des données qui ne semblent
  pas être un index, `low` si elle est entièrement vide ou si ses valeurs
  forment une séquence d'entiers consécutifs.
- **Pourquoi** : une colonne sans nom (`Unnamed: N`, artefact pandas/Excel, ou
  nom vide/blanc) signale généralement un décalage de colonnes à l'export ou
  un en-tête mal renseigné. Si elle ne contient aucune donnée, c'est un simple
  artefact d'export sans impact (sévérité réduite).
- **Cas particulier découvert en calibrant sur données réelles** :
  `NL_B2C_AYV_OFF.xlsx` (`LOAD_LOCK_202604`) a une colonne `Unnamed: 0` dont
  les 17171 valeurs sont exactement `0, 1, 2, ..., 17170` — l'index pandas
  exporté par erreur (`to_csv`/`to_excel` sans `index=False`), pas un
  décalage de colonnes. `_est_index_exporte` détecte ce motif (séquence
  d'entiers consécutifs, dans l'ordre) et ramène la sévérité à `low` : c'est
  un artefact d'export inoffensif pour le reste du fichier, pas un signe
  fiable de corruption.

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

- **Sévérité** : `low` si le taux de valeurs manquantes est ≥ 50%,
  `medium` si ≥ 90%.
- **Pourquoi** : une colonne majoritairement vide est peu fiable pour toute
  analyse statistique ou métier ultérieure. Les seuils sont des constantes
  (`SEUIL_NAN_MEDIUM`, `SEUIL_NAN_HIGH`) en haut du fichier, ajustables sans
  toucher à la logique.
- Exclut les colonnes sans nom et les colonnes à 100% de NaN, déjà couvertes
  par les deux vérifications précédentes.
- **Sévérités volontairement non bloquantes, révisées après calibration sur
  données réelles** : à l'origine `high` au-delà de 90%, ce qui suffisait à
  lui seul à faire REJETER un fichier (voir `judge/regles_judge.md`, plafond
  des sévérités non bloquantes). Sur `LOAD_LOCK_202604` (fichiers déjà
  validés par des experts), 3 fichiers étaient rejetés uniquement à cause
  d'une colonne à >90% de NaN — dans les trois cas un champ optionnel
  légitime (`ranking`/`promotionText` d'AYV_OFF, `CO2` de SUZ_OFF pour des
  véhicules électriques, `label_direct` d'athlon_B2B_2), pas un export
  cassé. Descendu à `medium`/`low` : un taux de NaN élevé reste une
  observation purement statistique, sans le contexte métier nécessaire pour
  savoir si le champ est censé être rempli. `verifier_colonnes_sans_nom` et
  `verifier_colonnes_dupliquees` restent `high`, eux, car ce sont des signes
  univoques d'un problème d'export, pas de simples champs creux.

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
