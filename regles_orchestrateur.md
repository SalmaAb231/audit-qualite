# Règles de l'orchestrateur

Ce document décrit `orchestrateur.py`, le chef de gare du pipeline :
`structure/` (portier) → `stats/` **et** Cohérence **en parallèle** (chacun
sur tout le fichier) → `Réalisme` (lignes suspectes pointées par les trois
détecteurs précédents + échantillon de contrôle) → `judge/` (verdict) →
`reporting/` (rapport), puis un rapport consolidé sur le run entier.

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
   sous-dossier `resultats/runs/<run_id>/<dossier_mois>/` par run et mois (voir
   `_sauvegarder_resultats_fichier`), et le comportement "toutes les copies
   mensuelles d'un nom" est documenté explicitement plutôt qu'implicite.
3. **Appeler Réalisme sur tout le fichier contredit l'architecture déjà
   décidée** (Étape 4 : "seulement sur les lignes suspectes") et aurait
   épuisé le quota gratuit (45 requêtes/jour) en quelques fichiers à peine,
   sur ~200 fichiers réels du projet. Voir la section suivante.

## Sélection des lignes envoyées à Réalisme (`_lignes_suspectes`)

Réalisme reçoit : les lignes explicitement pointées par une anomalie de
`structure/`, `stats/` ou Cohérence (champ `ligne`, **y compris `ligne=0`**) + un
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
Quand il appelle Réalisme, l'orchestrateur passe désormais
`echantillonner=False` : il est l'unique responsable du lot envoyé et aucune
ligne explicitement suspecte ne peut disparaître dans un second tirage.
L'échantillonnage historique de `agents/agent_realisme.py` reste disponible
uniquement pour l'usage autonome de cet agent.

## Cohérence en parallèle avec Stats

`pipeline_fichier()` lance `stats.agent_stats.analyser()` et
`AgentCoherence().analyser_fichier()` **dans deux threads** (`ThreadPoolExecutor`,
2 workers) juste après le garde de structure, et attend les deux avant de
continuer — deux canaux indépendants qui lisent chacun tout le fichier, sans
que l'un filtre l'autre (Cohérence ne reçoit jamais que les lignes suspectes
de Stats, ni l'inverse). Leurs anomalies combinées alimentent à la fois
`_lignes_suspectes()` (pour Réalisme) et `toutes_anomalies` (pour le
verdict final).

**Pourquoi des threads et pas juste un appel séquentiel** : Stats est
quasi-instantané (pandas, pas de LLM) alors que Cohérence peut prendre
plusieurs minutes (plusieurs appels LLM en chunks) — les lancer en parallèle
évite d'attendre Stats-puis-Cohérence en série pour rien, sans risque de
concurrence entre eux (Stats ne touche jamais le compteur de quota LLM ni le
cache réseau, seul Cohérence le fait).

**Deux redondances corrigées** : `AgentCoherence.analyser_fichier()`
rechargeait elle-même le fichier depuis le disque et ré-exécutait son propre
garde d'entrée `structure/`, en double avec ce que `pipeline_fichier()`
avait déjà fait juste avant — notable sur les plus gros fichiers du projet
(plusieurs centaines de milliers de lignes). Corrigé en ajoutant deux
paramètres optionnels à `analyser_fichier(chemin, df=None,
anomalies_structure=None)` : s'ils sont fournis (cas de l'orchestrateur),
aucun rechargement ni double garde ; s'ils sont absents (usage autonome,
ex. `agents/test_coherence.py`), comportement inchangé. Modification faite
dans `agents/agent_coherence.py` (fichier de Salma) à la demande de Hanane —
à lui signaler, elle n'était pas au courant que l'orchestrateur l'appelle
désormais automatiquement. Voir la docstring de `analyser_fichier`.

**Coût en requêtes, à surveiller** : contrairement à Réalisme (limité aux
lignes suspectes), Cohérence traite tout le fichier en chunks
(`config.CHUNK_SIZE`) — un fichier de plusieurs milliers de lignes peut
coûter plusieurs dizaines de requêtes à lui seul (voir le commentaire de
`config.CHUNK_SIZE` : "~3 400 appels pour le run Cohérence complet" sur
l'ensemble du projet). Avec `config.RATE_LIMIT["max_requetes_jour"] = 45`,
un run multi-fichiers via `orchestrateur.main()` peut désormais épuiser le
quota sur un seul gros fichier — le garde-fou de quota (section suivante)
protège contre les conséquences (verdicts faussement propres), mais ne
change pas le fait qu'on traite beaucoup moins de fichiers par jour
qu'avant l'ajout de Cohérence.

**Limite connue restante** :
- `stats.verifier_outliers_numeriques` n'inclut dans son anomalie que
  l'index du **premier** outlier de chaque colonne (voir son docstring et
  `stats/regles.md`) : les outliers suivants de la même colonne ne
  deviennent pas des lignes suspectes via ce mécanisme, seulement via
  l'échantillon aléatoire (probabilité, pas garantie).

## Vérification web de Réalisme

Réalisme vérifie désormais ses anomalies candidates par une recherche web
(bibliothèque `ddgs`, gratuite, sans clé API) avant de les retenir
définitivement — pas du tool-calling agentique (peu fiable sur les modèles
`:free`), un second passage fixe après le jugement initial. Détails complets
dans `agents/regles_realisme.md` ("Vérification web (pas de tool calling)").

## Garde-fou de quota

`main()` vérifie `agents.llm_client.quota_restant()` avant chaque fichier et
`pipeline_fichier()` le revérifie avant Réalisme. Cohérence et Réalisme
remontent chacun un statut `SUCCESS`, `PARTIAL` ou `FAILED` avec leurs
compteurs de chunks. Si une étape LLM obligatoire n'est pas complète, le
verdict porte `pipeline_status="INCOMPLETE"` et le Judge retourne
`verdict=null`, `score=null`, `grade=null` au lieu d'un résultat qualité
faussement rassurant. Les anomalies trouvées par les chunks réussis restent
conservées pour diagnostic.

## Ce que ce module ne fait pas

Il ne provisionne pas l'infrastructure AWS. En mode S3, il publie les rapports
et verdicts sous `reports/runs/<run_id>/`, l'historique sous
`history/runs/YYYY/MM/DD/` et copie la source sous `quarantine/<mois>/` pour
un verdict `REJECTED` ou un statut `INCOMPLETE`. Il conserve aussi les sorties
locales ; un échec d'upload est journalisé sans modifier automatiquement le
statut métier. En mode LOCAL, aucune copie de quarantaine dédiée n'est créée.

Il ne reprend pas un run interrompu où il
s'était arrêté (pas de checkpoint façon "reprendre après le fichier N") :
chaque exécution reparcourt tout ce qu'on lui donne depuis le début. Cette
limite ne concerne pas les chunks de Cohérence : leur checkpoint réutilise les
chunks déjà réussis et retente uniquement les chunks absents ou en échec. Voir
`docs/fiabilite_pipeline.md` pour l'identité et les règles de reprise.
