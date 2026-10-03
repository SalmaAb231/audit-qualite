# Règles de détection statistique

Ce document liste les vérifications effectuées par `stats/agent_stats.py`,
le deuxième détecteur du pipeline d'audit. Il juge la **plausibilité
statistique des valeurs**, jamais la forme du fichier (rôle de `structure/`)
ni la cohérence métier au sens large (rôle futur des agents LLM Cohérence et
Réalisme) — sauf le check 5, qui capture mathématiquement un cas particulier
d'incohérence métier détectable sans connaître le sens des colonnes.

Toutes les anomalies produites ont `type: "statistique"` et `source: "stats"`
(détection déterministe, sans LLM). Convention sur le champ `ligne` : `0`
signifie que l'anomalie concerne une colonne entière, pas une ligne précise.

## `verifier_outliers_numeriques`

- **Sévérité** : `medium`
- **Pourquoi** : une valeur très éloignée du reste d'une colonne numérique
  (méthode IQR, bornes `[Q1 - 1.5×IQR, Q3 + 1.5×IQR]`) peut signaler une
  erreur de saisie ou de scraping (ex. un kilométrage à 250 000 alors que le
  reste du fichier est entre 500 et 42 000).
- Si plus de 30% des valeurs d'une colonne sont hors bornes
  (`SEUIL_MAX_PART_OUTLIERS`), la colonne n'est probablement pas unimodale
  (ex. plusieurs familles de véhicules mélangées) : la méthode IQR n'est pas
  fiable dans ce cas, le check est ignoré pour cette colonne plutôt que de
  produire une avalanche de faux positifs.

## `verifier_valeurs_negatives_minoritaires`

- **Sévérité** : `medium`
- **Pourquoi** : une colonne numérique presque toujours positive (`>= 90%`)
  qui contient malgré tout quelques valeurs négatives (`< 10%`) suggère une
  erreur de saisie ou un problème d'unité, plutôt qu'une convention de signe
  volontaire. Une colonne majoritairement négative (`>= 10%` de négatifs)
  n'est pas signalée : elle a probablement un sens de signe intentionnel
  (ex. une colonne "écart" ou "ajustement").

## `verifier_colonnes_quasi_constantes`

- **Sévérité** : `low`
- **Pourquoi** : une colonne où une seule valeur représente `>= 99%` des
  lignes (avec au moins 20 valeurs, pour ne pas juger sur un petit
  échantillon) est souvent peu informative, ou révèle un artefact de
  scraping (même valeur copiée partout par erreur).

## `verifier_cardinalite_suspecte`

- **Sévérité** : `low`
- **Pourquoi** : une colonne texte où presque toutes les valeurs sont
  uniques (`>= 95%`, avec au moins 50 valeurs) est rarement adaptée à une
  analyse catégorielle.
- **Risque connu de faux positif** : l'inventaire du projet
  (`resultats/inventaire.csv`) montre de nombreuses colonnes légitimement
  quasi-uniques par nature (URL, identifiants, références). Ce check exclut
  les colonnes dont le nom contient `url`, `id`, `ref`, `lien`, `code`,
  `key`, `sku` ou `vin` (`NOMS_IDENTIFIANTS_RE`), et reste volontairement en
  sévérité `low` avec une confiance modérée (0.5) : informatif plutôt
  qu'alarmant.

## `verifier_incoherence_entre_colonnes`

- **Sévérité** : `medium`
- **Pourquoi** : les vérifications ci-dessus regardent une colonne à la
  fois. Elles ne peuvent pas voir qu'une "petite voiture" a un "prix
  énorme" : ni la puissance seule ni le prix seul n'est un outlier, seule
  leur combinaison l'est. Ce check compare deux colonnes numériques
  corrélées entre elles (ex. puissance et prix) : si le fichier montre une
  relation habituelle entre les deux (corrélation de rang de Spearman
  `>= 0.7` en valeur absolue), on ajuste une régression simple et on repère
  les lignes dont le résidu est lui-même un outlier (méthode IQR appliquée
  aux résidus, pas aux valeurs brutes, avec un facteur plus strict — voir
  ci-dessous). La sélection des paires utilise Spearman plutôt que Pearson :
  Pearson est sensible à une seule valeur extrême, or c'est justement ce
  qu'on cherche à détecter — une ligne isolée suffirait à faire chuter
  artificiellement une corrélation de Pearson et à faire ignorer la paire à
  tort.
- **Portée volontairement limitée** : uniquement entre colonnes *numériques*
  corrélées. Une incohérence entre une colonne texte et une colonne
  numérique (ex. "Carburant = Électrique" avec une consommation d'essence
  renseignée) reste hors de portée d'une régression et nécessite un agent
  LLM (Cohérence) pour être détectée.
- **Limite connue, découverte en testant sur données réelles** : une
  régression ajustée sur tout le fichier suppose une population homogène.
  Sur `NL_B2B_ABT_OFF.xlsx`, la première version de ce check (corrélation
  `>= 0.5`, IQR standard à 1.5×) remontait 735 anomalies sur 2548 lignes —
  toutes des véhicules électriques, qui ont une relation prix/caractéristiques
  différente des véhicules thermiques majoritaires dans le fichier : un
  sous-groupe cohérent, pas une erreur de donnée. Resserrer le seuil de
  corrélation ne suffisait pas à éliminer ce bruit (98 lignes du même
  sous-groupe restaient signalées même à corrélation `>= 0.8`). La solution
  retenue : un facteur IQR spécifique aux résidus, `FACTEUR_IQR_RESIDUS = 3.5`
  (contre `FACTEUR_IQR = 1.5` pour les outliers bruts) — un sous-groupe
  cohérent produit un décalage modéré mais systématique, alors qu'une
  vraie incohérence isolée (ex. l'exemple "petite voiture, prix énorme")
  produit un résidu bien plus extrême. Validé sur 3 fichiers réels de tailles
  différentes (2548, 2798 et 159 lignes) : 0 anomalie restante due à ce
  sous-groupe, tout en continuant à détecter une incohérence isolée injectée
  artificiellement dans les tests.
- Au plus 20 paires de colonnes sont analysées par fichier
  (`NB_PAIRES_MAX_TRAITEES`, les plus corrélées d'abord), pour bornir le
  coût sur les fichiers à beaucoup de colonnes numériques ; un dépassement
  est journalisé (jamais silencieux).
- Une ligne qui casse plusieurs paires de colonnes ne produit qu'**une
  seule** anomalie agrégée (toutes les colonnes impliquées listées
  ensemble), pas une par paire cassée.
- **Deuxième limite découverte en testant sur données réelles, plus grave
  que la première** : sur un lot de 59 fichiers réels de `LOAD_LOCK_202604`
  (considérés sans problème, déjà validés par des experts), ce check
  produisait jusqu'à 13 437 anomalies sur un seul fichier
  (`NL_B2B_DIL_ALL_PART1.xlsx`, 213 220 lignes) et 3 378 sur un autre
  (`NL_B2B_AYV_OFF.xlsx`, 25 572 lignes) — assez pour saturer le score du
  Judge à 0 quel que soit le contenu réel du fichier. Cause identifiée :
  une colonne quasi-catégorielle encodée en nombre (ex. `engineCylinders`,
  4 valeurs distinctes : 0/3/4/6 ; ou un code "Tank- en/of laadpas", 2
  valeurs) affiche une corrélation de rang très élevée (0.92) avec une
  autre colonne discrète sans aucune relation métier réelle — l'IQR des
  résidus de la régression devient quasi nul, donc les bornes
  `[-0.01, 0.05]` transforment n'importe quelle petite variation normale
  en anomalie (10% des lignes flaggées pour une seule paire). Corrigé en
  excluant les colonnes à faible cardinalité (`SEUIL_CARDINALITE_MIN_PAIRE
  = 10` valeurs distinctes minimum) de ce check, en plus des colonnes
  booléennes (mêmes symptômes, cardinalité 2).
- **Effet de bord accepté** : même après ce correctif, des paires de
  colonnes réellement continues mais quasi-redondantes (ex. `Power_kW` /
  `Power_hp`, deux unités de la même grandeur physique) peuvent encore
  produire quelques milliers d'anomalies sur les plus gros fichiers
  (`Power_kW` vs `FiscalValue` : 3 720 lignes sur 213 220, soit 1.7%) —
  chaque écart individuel (probablement un arrondi) reste statistiquement
  correct à signaler, mais le **volume brut** croît avec la taille du
  fichier sans rapport avec la gravité réelle du problème. Ce n'est plus
  corrigé ici : voir `judge/regles_judge.md` (plafond de pénalité) pour la
  solution retenue — normaliser au niveau du calcul de score plutôt que de
  supprimer des anomalies individuellement correctes.

## Ce que ce module ne fait pas

Les doublons exacts de lignes sont déjà couverts par
`structure/verifier_lignes_dupliquees` : pas de vérification équivalente
ici. `stats/` ne définit pas non plus de notion de "fichier inexploitable"
(pas de sévérité `critical`) : cette décision reste le rôle exclusif de
`structure/fichier_exploitable()`.
