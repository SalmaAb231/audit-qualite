# Règles de l'agent Judge

Ce document décrit `judge/agent_judge.py`, le point de convergence du
pipeline : il ne détecte rien lui-même, il agrège les anomalies déjà
produites par `structure/`, `stats/`, et les agents LLM (`Cohérence`,
`Réalisme`), toutes conformes à `docs/contrat_anomalie.md`. 100%
déterministe, aucun appel LLM — voir la section "Justification" plus bas
pour pourquoi.

## Formule de score

Pour chaque anomalie : poids = `config.PONDERATION_SEVERITE[severite]`,
doublé si `severite == "critical"` (`config.FACTEUR_PENALITE_CRITICAL`).
Les poids `high`/`critical` s'additionnent sans limite ; les poids
`medium`/`low` s'additionnent séparément mais leur somme est plafonnée à
`config.PLAFOND_PENALITE_NON_BLOQUANTE` (voir plus bas, correctif ajouté
après calibration sur données réelles). `score = 1.0 - (pénalité grave +
pénalité non bloquante plafonnée)`, borné à `[0.0, 1.0]`. Grade (A-F) et
verdict (ACCEPTED/WARNING/REJECTED) dérivés du même score via
`config.SEUILS_GRADE` et `config.SEUIL_ACCEPTED`/`SEUIL_WARNING`.

## Correctif ajouté après calibration sur données réelles : plafond des sévérités non bloquantes

Calibration effectuée sur 59 fichiers réels de `LOAD_LOCK_202604`, déjà
validés par des experts métier (donc attendus sans problème bloquant).
Avec la formule initiale (pas de plafond), **50 fichiers sur 59 (85%)**
sortaient REJECTED/score=0, y compris des fichiers avec seulement 3
anomalies `medium`/`low` (ex. `NL_B2B_LYS_OFF.xlsx` : 3 anomalies medium →
pénalité 1.2 → score 0). Cause : `stats/` ne produit jamais de sévérité
`high`/`critical` (voir `stats/regles.md`) — sur un fichier à structure
saine, tout le poids vient donc de `medium`/`low`, qui s'accumulait sans
limite. Un fichier de 213 220 lignes avec un taux d'anomalies statistiques
de seulement 1.7% (voir `stats/regles.md`, deuxième limite) atteint déjà
plusieurs milliers d'anomalies en valeur absolue, largement assez pour
saturer le score à 0 — un artefact de la taille du fichier, pas de sa
qualité réelle.

**Correctif** : `config.PLAFOND_PENALITE_NON_BLOQUANTE = 0.25` plafonne la
pénalité cumulée `medium`/`low`. Conséquence directe et recherchée :
`stats/` seul (qui ne produit jamais `high`/`critical`) ne peut désormais
plus jamais faire descendre un fichier sous WARNING — cohérent avec le
principe déjà établi ailleurs dans le projet ("`stats/` ne décide jamais
qu'un fichier est inexploitable", rôle réservé à `structure/`, voir
`stats/regles.md` — "Ce que ce module ne fait pas"). Seule une anomalie
`high` ou `critical` (structure, ou plus tard Cohérence/Réalisme) peut
désormais faire chuter un fichier jusqu'à REJECTED.

## Limites restantes, découvertes en implémentant la formule initiale

Ces deux points restent vrais après le correctif ci-dessus (qui ne change
que le traitement `medium`/`low`) — observations arithmétiques, pas des bugs :

1. **Le grade B (0.85-0.94) est inatteignable en pratique.** Le poids le
   plus faible (`low` = 0.2) est déjà supérieur à l'écart entre 1.0 et 0.85
   (0.15). Dès qu'une seule anomalie existe, même la plus mineure, le score
   tombe au mieux à 0.8 — directement dans la tranche C, sans jamais
   traverser B. Seul un fichier à zéro anomalie obtient A ; le grade
   immédiatement inférieur observable est C, jamais B.
2. **Le doublement de `critical` (`FACTEUR_PENALITE_CRITICAL`) n'a aucun
   effet observable sur le résultat final.** `PONDERATION_SEVERITE["critical"]`
   vaut déjà 1.0 : une seule anomalie critical, même sans doublement, sature
   déjà la pénalité "grave" au maximum utile (score = 0.0, puisque non
   plafonnée). Doubler à 2.0 ne change rien puisque le score est de toute
   façon borné à 0.0 dans les deux cas. Vérifié avec l'exemple de calcul
   fourni à l'origine (critical + high + medium = pénalité grave 2.7 +
   medium plafonné 0.25 = 2.95 avec doublement) : sans doublement, pénalité
   grave 1.7 + 0.25 = 1.95 — dans les deux cas, score = 0.0, verdict
   REJECTED, grade F.

Non corrigés : partagés avec l'équipe pour décider si la granularité des
poids ou des seuils de grade doit encore évoluer, pas une correction
unilatérale supplémentaire.

## Justification 100% déterministe, pas de LLM

La signature demandée pour `_generer_justification(verdict, anomalies)` ne
prend pas de client LLM, et une phrase de synthèse (compte par sévérité +
exemple le plus grave) n'a pas besoin d'un modèle pour être correcte et
lisible — contrairement à Cohérence/Réalisme qui doivent juger un contenu
métier. Aucun appel réseau, donc aucune consommation du quota partagé
(`config.RATE_LIMIT`), et une sortie 100% reproductible pour les tests.
Une reformulation par le modèle léger de `config.MODELES["judge"]` reste une
extension possible si une prose plus travaillée est souhaitée, mais n'est
pas implémentée ici.

## Ce que ce module ne fait pas

Il ne lit jamais le champ `source` d'une anomalie : le score traite une
anomalie `stats` et une anomalie `llm` de sévérité identique de façon
strictement identique. Il ne recharge pas non plus les fichiers de données
ni ne recalcule aucune anomalie : il attend une liste déjà produite et
conforme au contrat, construite par l'orchestrateur (pas encore écrit) en
concaténant les listes de tous les détecteurs pour un même fichier.
