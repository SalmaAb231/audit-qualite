# CLAUDE.md

Ce fichier donne des instructions à Claude Code (claude.ai/code) pour travailler dans ce dépôt.

## Présentation

Un pipeline d'audit de qualité pour des fichiers Excel/CSV décrivant des
données de renting automobile (location longue durée de véhicules). Chaque
fichier passe par une série de détecteurs indépendants qui produisent des
**anomalies** dans un format JSON commun ; ces anomalies sont agrégées en un
**verdict** par fichier (`ACCEPTED` / `WARNING` / `REJECTED`) puis mises en
forme dans des **rapports HTML**. Développement et exécution actuels en
local ; déploiement futur visé sur AWS (S3 + EC2). La documentation et les
commentaires du code sont en français.

## Commandes

```powershell
# Installation
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # puis renseigner sa propre clé OPENROUTER_API_KEY

# Lancer le pipeline complet
python -m orchestrateur                          # tous les fichiers de DATA_DIR
python -m orchestrateur NL_B2B_BRO_OFF.xlsx       # fichier(s) nommé(s) (toutes les copies mensuelles trouvées)

# Lancer un détecteur individuellement
python -m structure.agent_structure <fichier_ou_chemin>
python -m stats.agent_stats <fichier_ou_chemin>
python -m agents.agent_realisme <fichier_ou_chemin>
python -m judge.agent_judge <chemin_json_anomalies>
python -m reporting.agent_reporting fichier <chemin_verdict.json>
python -m reporting.agent_reporting consolide [dossier_resultats]

# Utilitaires d'ingestion
python -m ingestion.profiling            # régénère resultats/inventaire.csv
python -m ingestion.test_s3_connexion    # diagnostic de connexion S3 avant déploiement

# Tests
python -m pytest tests/ -v
python -m pytest tests/test_stats.py -v
python -m pytest tests/test_stats.py::test_nom_de_la_fonction -v
```

`agents/agent_coherence.py` n'a pas de CLI : s'utilise via
`AgentCoherence().analyser_fichier(chemin)` (voir `agents/test_coherence.py`).
Ses tests automatisés de checkpoints et de statuts vivent dans
`tests/test_agent_coherence.py`. Les scripts historiques de validation
manuelle restent à côté du code (`agents/test_coherence.py`,
`agents/test_coherence_injection.py`).

Aucun test ne fait de véritable appel réseau/LLM — les tests LLM utilisent
un client factice (`_ClientFactice` dans `tests/test_agent_realisme.py` et
`tests/test_orchestrateur.py`). Deux fixtures `autouse` protègent contre les
fuites accidentelles : `config.RECHERCHE_WEB_ACTIVEE` désactivée par défaut,
et les dossiers de résultats/checkpoints de Cohérence redirigés vers
`tmp_path`.

## Architecture

### Le pipeline (orchestré par `orchestrateur.py`)

```
Fichier (Excel/CSV)
  1. structure/                         — la forme du fichier est-elle valide ? (colonnes, doublons, types)
     une anomalie "critical" ici arrête tout pour ce fichier : REJECTED direct, aucun coût LLM
  2. stats/  ET  agents/agent_coherence.py   — EN PARALLÈLE (deux canaux indépendants, même df déjà chargé)
     stats/     : valeurs statistiquement plausibles ? (IQR, corrélations) — tout le fichier, 100% mathématique, aucun LLM
     Cohérence  : contradiction logique entre colonnes d'une même ligne ? — tout le fichier, LLM
  3. agents/agent_realisme.py            — les valeurs ont-elles un sens dans le monde réel automobile ?
     LLM, uniquement sur les lignes repérées comme suspectes aux étapes 1-2 + un échantillon aléatoire de contrôle,
     puis vérifie ses propres anomalies candidates par recherche web gratuite (ddgs)
  4. judge/                              — agrège TOUTES les anomalies (peu importe qui les a trouvées) -> score, grade, verdict
  5. reporting/                          — rapport HTML par fichier + rapport consolidé sur tout le run
```

Se lance via `orchestrateur.main()` / `python -m orchestrateur`. Les
décisions de conception et les cas particuliers sont documentés dans
`regles_orchestrateur.md` — à lire avant de modifier la logique
d'orchestration. Points clés :

- Stats et Cohérence tournent dans un `ThreadPoolExecutor` (2 workers) juste
  après le garde de structure ; Cohérence reçoit le `df` déjà chargé et les
  anomalies de structure déjà calculées, donc elle ne recharge jamais le
  fichier ni ne revérifie elle-même son exploitabilité (voir
  `_analyser_coherence` dans `orchestrateur.py` et la docstring de
  `AgentCoherence.analyser_fichier`).
- `_lignes_suspectes()` sélectionne les lignes envoyées à Réalisme : toute
  ligne pointée par une anomalie de structure/stats/cohérence (champ
  `ligne`, **y compris `ligne=0`** — volontairement non exclue, voir
  `regles_orchestrateur.md` pour le vrai bug que ça a corrigé) plus un
  échantillon aléatoire de contrôle (`config.TAUX_ECHANTILLON_CONTROLE`).
- Les résultats sont écrits par mois d'origine
  (`resultats/LOAD_LOCK_AAAAMM/`) pour éviter toute collision entre des
  fichiers de même nom dans des mois différents — plusieurs fichiers réels
  existent dans plusieurs dossiers `data/LOAD_LOCK_*/`.
- `main()` vérifie `agents.llm_client.quota_restant()` avant chaque fichier
  et arrête proprement le run à 0 restant, plutôt que de laisser Réalisme
  produire silencieusement "aucune anomalie" à cause du blocage interne de
  quota (ce qui donnerait un verdict ACCEPTED propre en apparence mais faux).
- Cohérence et Réalisme remontent `SUCCESS`, `PARTIAL` ou `FAILED`. Si l'un
  des deux n'est pas complet, le Judge reçoit `pipeline_status="INCOMPLETE"`
  et ne publie ni score, ni grade, ni verdict. Voir
  `docs/fiabilite_pipeline.md` avant de modifier cette propagation.

### Choix de conception : schema-agnostic

Les fichiers source proviennent de loueurs/constructeurs différents, avec
des structures de colonnes différentes et imprévisibles. Le projet ne fait
donc **aucun mapping de schéma ni normalisation de colonnes**, nulle part.
Chaque fichier est traité tel quel avec ses colonnes réelles : les
vérifications mathématiques (`structure/`, `stats/`) s'adaptent
dynamiquement à n'importe quelle colonne numérique/texte, et les agents LLM
(Cohérence, Réalisme) lisent les noms de colonnes réels sans schéma
imposé. Ne pas introduire de schéma canonique ni de couche de
renommage de colonnes.

### Le contrat commun d'anomalie

Tous les détecteurs (déterministes ou LLM) produisent des anomalies dans le
même format JSON, défini dans `docs/contrat_anomalie.md` :

```json
{
  "fichier": "NL_B2B_ABT_OFF.xlsx",
  "ligne": 42,
  "colonnes": ["Price"],
  "type": "realisme",
  "severite": "critical",
  "source": "llm",
  "valeur": "8699956",
  "justification": "Un loyer mensuel de 8 699 956€ est irréaliste pour ce véhicule.",
  "confiance": 0.99
}
```

- `type` : `structure` / `statistique` / `coherence` / `realisme` — qui a détecté.
- `severite` : `critical` / `high` / `medium` / `low` — pondérée dans `config.py`.
- `source` : `stats` (déterministe) ou `llm`.
- `ligne == 0` est ambiguë par convention (peut signifier "portée colonne,
  pas une ligne précise" OU un vrai index de ligne 0) — voir la gestion de
  l'orchestrateur ci-dessus avant de supposer laquelle s'applique à une
  anomalie donnée.

C'est ce contrat commun qui permet à `judge/` d'agréger les anomalies de
toutes les sources sans traitement spécifique, et à `reporting/` de les
afficher de façon uniforme. Tout nouveau détecteur doit respecter
exactement ce format.

### `config.py` — source unique de vérité

Tout seuil, tout chemin, tout paramètre LLM passe par ce fichier ; aucun
module ne définit ses propres constantes de configuration. Notamment :
- `DATA_DIR`, `RESULTATS_DIR`, `CACHE_LLM_DIR` : chemins.
- `MODELES`, `TEMPERATURES`, `MAX_TOKENS` : un jeu de modèles OpenRouter par
  agent (le premier = principal, les suivants = cascade de fallback), tous
  en tier `:free`. Vérifier régulièrement
  https://openrouter.ai/models (filtre FREE) — le catalogue `:free` tourne
  sans préavis.
- `RATE_LIMIT`, `CHUNK_SIZE`, `MAX_LIGNES_PAR_FICHIER`,
  `TAUX_ECHANTILLON_CONTROLE` : contraintes du tier gratuit et découpage
  des données envoyées aux agents LLM.
- `PONDERATION_SEVERITE`, `FACTEUR_PENALITE_CRITICAL`, `SEUILS_GRADE`,
  `PLAFOND_PENALITE_NON_BLOQUANTE`, `SEUIL_ACCEPTED`, `SEUIL_WARNING` :
  tout ce dont dépend la formule de score/grade/verdict dans `judge/`.

### Le socle LLM commun à tous les agents (`agents/`)

- **`llm_client.py`** : client HTTP OpenRouter partagé. Gère la cascade de
  modèles (`config.MODELES`), les retries progressifs, le cache disque des
  réponses (`cache_llm/`, pour ne jamais repayer une requête identique) et
  le respect strict du quota gratuit quotidien (`quota_restant()`).
- **`parse_utils.py`** : extraction robuste de JSON depuis une réponse LLM
  (potentiellement mal formée, tronquée, entourée de texte) et validation
  contre le contrat commun d'anomalie.
- **`agent_coherence.py`** : contradictions logiques entre colonnes d'une
  même ligne. Lit tout le fichier (échantillonné seulement au-delà de
  `config.SEUIL_FICHIER_GEANT`), gère son propre checkpoint
  (`resultats/coherence/checkpoints/`, reprise sans re-consommer de
  requête), sauvegarde son résultat complet dans
  `resultats/coherence/<fichier>.json`.
  L'identité combine chemin source relatif, SHA-256 du contenu et version de
  checkpoint ; seuls les chunks réussis sont réutilisés.
- **`agent_realisme.py`** : une valeur a-t-elle un sens dans le monde réel
  automobile (par opposition à la contradiction entre colonnes de
  Cohérence, ou à l'aberration purement statistique de stats/) ? Découpe
  les lignes en chunks (`config.CHUNK_SIZE`), injecte la date du jour dans
  le prompt (sinon le modèle ne peut pas savoir si une date est future ou
  passée), ne décide jamais lui-même quelles lignes analyser — c'est le
  rôle de l'orchestrateur via `_lignes_suspectes`. Vérifie ensuite ses
  propres anomalies candidates par un appel LLM de vérification par chunk
  (pas par anomalie) via recherche web (`ddgs`/DuckDuckGo, gratuite, sans
  clé API) — pilotable via `config.RECHERCHE_WEB_ACTIVEE`.

### Les fichiers `regles*.md` par module

Chaque étape du pipeline a son propre document de règles/limites à côté du
code (`structure/regles.md`, `stats/regles.md`,
`agents/regles_realisme.md`, `judge/regles_judge.md`,
`reporting/regles_reporting.md`, `regles_orchestrateur.md`). Ils
consignent les décisions de calibration et les limites connues découvertes
en testant sur données réelles (pourquoi tel seuil existe, pourquoi telle
limite d'un détecteur est acceptée plutôt que corrigée) — à lire avant de
modifier le comportement du module correspondant, car ils expliquent
généralement une version précédente qui semblait raisonnable mais a échoué
sur des données réelles.

### `ingestion/`

`LocalLoader` (`local_loader.py`, dev local) et `S3Loader` (`s3_client.py`,
déploiement AWS uniquement) exposent la même interface publique, ce qui
rend le reste du pipeline agnostique du support de stockage.
`profiling.py` génère `resultats/inventaire.csv` à titre purement
informatif — jamais utilisé pour unifier les schémas (voir "schema-agnostic"
ci-dessus).

### Dossiers non versionnés (`.gitignore`)

- **`data/`** : fichiers Excel/CSV réels à auditer, organisés par mois
  (`LOAD_LOCK_AAAAMM/`). Volumineux et potentiellement confidentiel —
  jamais commité, à récupérer séparément auprès de l'équipe.
- **`resultats/`** : toutes les sorties générées (`verdict_*.json`,
  `rapport_*.html`, `inventaire.csv`, etc.) — régénérées à chaque
  exécution, jamais commitées.
- **`cache_llm/`** : cache disque des réponses LLM et compteur de quota
  quotidien (`compteur_quotidien.json`) — état d'exécution local, jamais
  commité.

## Contraintes à connaître avant de lancer un run réel

- **Quota LLM partagé, très tendu** :
  `config.RATE_LIMIT["max_requetes_jour"] = 45` requêtes/jour, partagé par
  Cohérence et Réalisme. Le Judge est déterministe. Cohérence seule (tout le fichier, en
  chunks) peut coûter plusieurs dizaines de requêtes par fichier : un seul
  gros fichier peut épuiser le quota du jour sur un run multi-fichiers.
  `agents.llm_client.quota_restant()` donne le solde restant.
- **Un même nom de fichier peut exister dans plusieurs dossiers mensuels** —
  lancer un module directement par nom de fichier (ex.
  `python -m structure.agent_structure <nom>`) résout systématiquement vers
  la **première** copie trouvée ; donner un chemin complet pour viser une
  copie mensuelle précise. `orchestrateur.py`, lui, traite correctement
  chaque copie mensuelle séparément.
- **`ddgs` (recherche web de Réalisme) n'a pas d'API officielle** — peut se
  dégrader ou se bloquer sans préavis. Désactivable via
  `config.RECHERCHE_WEB_ACTIVEE = False` sans toucher au code si besoin.
