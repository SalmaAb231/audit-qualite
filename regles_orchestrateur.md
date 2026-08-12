# Règles de l'orchestrateur

Ce document décrit `orchestrateur.py`, le chef de gare du pipeline :
`structure/` (portier) → `stats/` (tout le fichier) → `Réalisme` (lignes
suspectes seulement) → `judge/` (verdict) → `reporting/` (rapport), puis un
rapport consolidé sur le run entier.

## Écart avec une première version proposée

Une première version de ce module (fournie comme point de départ) appelait
Réalisme sur **le fichier entier**, importait une fonction
`sauvegarder_rapports` qui n'existe pas dans `reporting/agent_reporting.py`,
et sélectionnait les fichiers par nom sans tenir compte des dossiers mensuels.
Trois corrections apportées, chacune vérifiée avant d'écrire le code
définitif :

1. **`sauvegarder_rapports` n'existe pas** (`grep` sur tout le dépôt : aucun
   résultat) — aurait fait planter l'import dès le premier `python -m
   orchestrateur`. `reporting/agent_reporting.py` expose seulement
   `generer_rapport_fichier`/`generer_rapport_consolide` (qui retournent du
   HTML) ; l'écriture sur disque est donc gérée ici, dans
   `_sauvegarder_resultats_fichier`.
2. **`NL_B2B_BRO_OFF.xlsx` et `NL_B2B_DAC_ALL.xlsx` existent chacun dans 3
   dossiers mensuels différents** (`LOAD_LOCK_202603/4/5`, vérifié avec `find`).
   Filtrer uniquement par nom (comme dans la version fournie) aurait traité
   chaque fichier 3 fois — triplant la consommation de requêtes LLM sans
   que ce soit visible dans les logs — et surtout **écrasé** les résultats
   entre les mois (même nom de fichier de sortie). Solution : un
   sous-dossier `resultats/<dossier_mois>/` par mois (voir
   `_sauvegarder_resultats_fichier`), et le comportement "toutes les copies
   mensuelles d'un nom" est documenté explicitement plutôt qu'implicite.
3. **Appeler Réalisme sur tout le fichier contredit l'architecture déjà
   décidée** (Étape 4 : "seulement sur les lignes suspectes") et aurait
   épuisé le quota gratuit (45 requêtes/jour) en quelques fichiers à peine,
   sur ~200 fichiers réels du projet. Voir la section suivante.

## Sélection des lignes envoyées à Réalisme (`_lignes_suspectes`)

Réalisme reçoit : les lignes explicitement pointées par une anomalie de
`structure/` ou `stats/` (champ `ligne`, **y compris `ligne=0`**) + un
échantillon aléatoire de contrôle (`config.TAUX_ECHANTILLON_CONTROLE`, 10%
par défaut) parmi les lignes restantes, pour repérer des problèmes qu'aucun
détecteur mathématique n'a signalés.

**Correctif après test réel : `ligne=0` n'est plus exclue.** La convention
"0 = portée colonne, pas une ligne précise" (`docs/contrat_anomalie.md`)
entre en collision avec un vrai index de ligne 0. Une première version de
`_lignes_suspectes` excluait `ligne=0` par précaution — mais ça a fait
manquer un vrai cas : le prix de 8 699 956€ injecté sur la ligne 0 de
`NL_B2B_VLV_OFF.xlsx` a été correctement détecté par `stats/` (`ligne=0`,
c'est réellement la ligne 0), mais jamais transmis à Réalisme, car exclu par
cette règle — et l'échantillon aléatoire de contrôle n'est pas tombé sur
cette ligne ce jour-là. Corrigé en incluant toujours `ligne=0` : le coût
d'inclure à tort la ligne 0 d'une anomalie réellement column-scope (une
ligne de plus, jamais dupliquée) est négligeable comparé au coût de
manquer un vrai problème.

**Limite connue restante, acceptée pour l'instant** :
- `stats.verifier_outliers_numeriques` n'inclut dans son anomalie que
  l'index du **premier** outlier de chaque colonne (voir son docstring et
  `stats/regles.md`) : les outliers suivants de la même colonne ne
  deviennent pas des lignes suspectes via ce mécanisme, seulement via
  l'échantillon aléatoire (probabilité, pas garantie).
- Cohérence n'existe pas encore dans le dépôt : elle ne contribue pas
  (encore) à la sélection. Prévu pour être ajoutée dans `pipeline_fichier()`
  dès que `agents/agent_coherence.py` existe.

Effet de bord positif observé : en pratique, `len(lot_suspect)` reste très
inférieur à `config.MAX_LIGNES_PAR_FICHIER` (2000) même sur les plus gros
fichiers du projet — cette limite de `agents/agent_realisme.py` reste un
filet de sécurité, plus le mécanisme réellement actif de réduction de coût.

## Garde-fou de quota

`main()` vérifie `agents.llm_client.quota_restant()` **avant** chaque
fichier et s'arrête proprement dès qu'il atteint 0, plutôt que de continuer
à appeler Réalisme jusqu'au blocage interne de `llm_client.py`. Raison :
un appel bloqué par quota à l'intérieur de `agent_realisme.py` est traité
comme "aucune anomalie sur ce chunk" (comportement voulu là-bas, pour ne
jamais faire planter un chunk isolé) — mais enchaîné sur plusieurs fichiers
consécutifs, cela produirait des verdicts ACCEPTED silencieusement faux
(Réalisme jamais vraiment exécuté, pas juste "rien trouvé"). Un arrêt net
avec un fichier de log explicite est préférable à un run complet mais
partiellement mensonger.

**Limite résiduelle acceptée** : si le quota s'épuise **pendant** le
traitement d'un fichier (entre deux chunks Réalisme du même fichier), les
chunks restants de ce fichier précis produisent quand même un verdict, sur
une couverture Réalisme incomplète, sans marqueur explicite de troncature
dans le verdict. Non traité ici : nécessiterait de faire remonter un
indicateur depuis `agents/agent_realisme.py` jusqu'au verdict, changement
qui dépasse le périmètre de cette itération.

## Ce que ce module ne fait pas

Il n'envoie rien vers S3 (`config.S3_REPORTS_PREFIX`) : tout est écrit en
local dans `resultats/`. Il ne reprend pas non plus un run interrompu où il
s'était arrêté (pas de checkpoint façon "reprendre après le fichier N") :
chaque exécution retraite tout ce qu'on lui donne depuis le début.
