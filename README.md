# audit-qualite

Pipeline d'audit de qualité pour des fichiers Excel/CSV de renting
automobile (location longue durée de véhicules). Chaque fichier passe par
une série de détecteurs indépendants qui produisent des **anomalies** dans
un format JSON commun, agrégées ensuite en un **verdict** par fichier
(`ACCEPTED` / `WARNING` / `REJECTED`) puis mises en forme dans des
**rapports HTML**. Déploiement futur sur AWS (S3 + EC2) ; développement et
tests actuels en local.

## Vue d'ensemble du pipeline

```
Fichier (Excel/CSV)
      │
      ▼
1. structure/     — la forme du fichier est-elle valide ? (colonnes, doublons, types)
      │  si anomalie "critical" → arrêt, verdict REJECTED direct, aucun coût LLM
      ▼
2. stats/          — les valeurs sont-elles statistiquement plausibles ? (IQR, corrélations)
      │  tourne sur TOUT le fichier, 100% mathématique, aucun LLM
      ▼
3. agents/agent_realisme.py — les valeurs ont-elles un sens dans le monde réel automobile ?
      │  LLM, mais UNIQUEMENT sur les lignes suspectes (repérées par 1-2) + échantillon de contrôle
      ▼
4. judge/          — agrège toutes les anomalies (peu importe qui les a trouvées) → score, grade, verdict
      ▼
5. reporting/      — rapport HTML par fichier + rapport consolidé sur tous les fichiers traités
      ▲
      └── orchestrateur.py fait circuler chaque fichier à travers ces 5 étapes
```

**Agent Cohérence** (incohérence logique entre plusieurs champs d'une même
ligne, ex. date de fin avant date de début) : prévu dans l'architecture,
**pas encore implémenté** dans ce dépôt (en cours de développement en
parallèle). L'orchestrateur tourne sans lui pour l'instant.

## Choix de conception : schema-agnostic

Les fichiers source ont des structures de colonnes différentes, et chaque
nouveau fichier peut avoir une structure inédite (noms de colonnes, ordre,
langue variables selon le loueur/constructeur). Le projet ne fait donc
**aucun mapping de schéma ni normalisation de colonnes** : chaque fichier
est traité tel quel, avec ses colonnes réelles. Les vérifications
mathématiques (`structure/`, `stats/`) s'adaptent dynamiquement à n'importe
quelle colonne numérique/texte ; les agents LLM (`Réalisme`, `Cohérence`)
lisent les noms de colonnes réels sans schéma imposé.

## Le contrat commun d'anomalie

Tous les détecteurs (déterministes ou LLM) produisent des anomalies dans le
**même format JSON**, défini dans [`docs/contrat_anomalie.md`](docs/contrat_anomalie.md) :

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
- `severite` : `critical` / `high` / `medium` / `low` — gravité, pondérée dans `config.py`.
- `source` : `stats` (déterministe) ou `llm`.

C'est ce contrat commun qui permet à `judge/` d'agréger les anomalies de
tous les détecteurs sans traitement spécifique par source, et à
`reporting/` de les afficher de façon uniforme.

## Structure détaillée du projet

### `config.py` (racine)

Configuration centralisée : **tout seuil, tout chemin, tout paramètre LLM
passe par ce fichier**, jamais codé en dur dans un module. Contient
notamment :
- `DATA_DIR`, `RESULTATS_DIR`, `CACHE_LLM_DIR` : chemins.
- `MODELES`, `TEMPERATURES`, `MAX_TOKENS` : un jeu de modèles OpenRouter par
  agent (avec cascade de fallback), tous `:free`.
- `RATE_LIMIT`, `CHUNK_SIZE`, `MAX_LIGNES_PAR_FICHIER`,
  `TAUX_ECHANTILLON_CONTROLE` : contraintes du tier gratuit OpenRouter et
  découpage des données envoyées aux agents LLM.
- `PONDERATION_SEVERITE`, `FACTEUR_PENALITE_CRITICAL`, `SEUILS_GRADE`,
  `PLAFOND_PENALITE_NON_BLOQUANTE`, `SEUIL_ACCEPTED`, `SEUIL_WARNING` :
  tout ce qui sert au calcul du score/grade/verdict dans `judge/`.

### `orchestrateur.py` (racine)

Le chef de gare : pour chaque fichier, enchaîne `structure/` → (arrêt si
`critical`) → `stats/` → sélection des lignes suspectes → `agent_realisme` →
`judge/` → `reporting/`, avec un garde-fou qui arrête proprement le run si
le quota LLM quotidien est épuisé (plutôt que de produire des verdicts
silencieusement faux). Un dossier de résultats séparé par mois d'origine
(`resultats/LOAD_LOCK_AAAAMM/`) évite toute collision entre deux fichiers de
même nom dans des mois différents. Détails et décisions de conception dans
[`regles_orchestrateur.md`](regles_orchestrateur.md).

```powershell
python -m orchestrateur                          # tous les fichiers de DATA_DIR
python -m orchestrateur NL_B2B_BRO_OFF.xlsx       # fichier(s) nommé(s)
```

### `structure/` — 1er détecteur : la forme du fichier

- **`agent_structure.py`** : vérifie que le fichier est bien formé
  (colonnes sans nom/dupliquées/vides, lignes dupliquées, taux de valeurs
  manquantes, types de données incohérents dans une colonne texte). 100%
  déterministe (pandas), aucun LLM. `fichier_exploitable()` décide si une
  anomalie `critical` doit stopper tout le reste du pipeline pour ce
  fichier (seul module à pouvoir prendre cette décision).
- **`regles.md`** : le détail de chaque vérification, ses seuils, et les
  limites découvertes en testant sur données réelles (ex. pourquoi un index
  pandas mal exporté ou un champ optionnel très creux ne sont plus classés
  `high`).

```powershell
python -m structure.agent_structure <fichier_ou_chemin>
```

### `stats/` — 2e détecteur : la plausibilité statistique

- **`agent_stats.py`** : 5 vérifications mathématiques sur les colonnes
  numériques (outliers via IQR, signes négatifs minoritaires, colonnes
  quasi-constantes, cardinalité suspecte) plus une vérification
  **multi-colonnes** (`verifier_incoherence_entre_colonnes`) qui détecte
  une incohérence entre deux colonnes numériques corrélées (ex. "petite
  voiture, prix énorme") sans connaître leur sens métier — via régression
  linéaire + détection d'outliers sur les résidus. Aucun LLM.
- **`regles.md`** : seuils et surtout les limites découvertes en testant
  sur données réelles (sous-populations de véhicules qui cassent une
  régression globale, colonnes à faible cardinalité qui produisent des
  milliers de faux positifs si on ne les exclut pas).

```powershell
python -m stats.agent_stats <fichier_ou_chemin>
```

### `agents/` — socle LLM commun + agent Réalisme

- **`llm_client.py`** : client HTTP pour l'API OpenRouter, partagé par tous
  les agents LLM (Réalisme, et à terme Cohérence/Judge/Reporting). Gère la
  cascade de modèles (`config.MODELES`), les retries progressifs, le cache
  disque des réponses (`cache_llm/`, pour ne jamais repayer une requête
  identique) et le respect strict du quota gratuit quotidien
  (`quota_restant()`).
- **`parse_utils.py`** : extraction robuste de JSON depuis une réponse LLM
  (qui peut être mal formée, tronquée, entourée de texte) et validation de
  chaque anomalie contre le contrat commun.
- **`agent_realisme.py`** : juge si une valeur a un sens dans le **monde
  réel automobile** (une marque/modèle/puissance/prix qui existe vraiment),
  par opposition à Cohérence (contradiction logique entre colonnes) ou
  `stats/` (aberration purement statistique). Découpe les lignes reçues en
  chunks (`config.CHUNK_SIZE`), injecte la date du jour dans le prompt
  (sans ça, le modèle ne sait pas si une date est dans le futur ou le
  passé). Ne décide jamais lui-même quelles lignes analyser : c'est
  `orchestrateur.py` qui lui fournit un sous-ensemble de lignes.
- **`regles_realisme.md`** : frontière avec Cohérence, limites connues
  (pas de recherche web, dépendance à la qualité du modèle gratuit du jour).
- **`test_premier_appel.py`** : script de preuve du socle LLM sur un
  fichier réel (historique, antérieur à `agent_realisme.py`).

```powershell
python -m agents.agent_realisme <fichier_ou_chemin>
```

### `judge/` — agrégation et verdict

- **`agent_judge.py`** : ne détecte rien, agrège les anomalies déjà
  produites par tous les détecteurs et calcule un score (0 à 1), un grade
  (A à F) et un verdict (`ACCEPTED`/`WARNING`/`REJECTED`), 100%
  déterministe. Une anomalie `critical`/`high` pèse sans limite ; les
  `medium`/`low` sont plafonnées (`config.PLAFOND_PENALITE_NON_BLOQUANTE`)
  pour qu'un détecteur purement statistique ne puisse jamais, à lui seul,
  rejeter un fichier.
- **`regles_judge.md`** : la formule de score en détail, et les
  découvertes de calibration sur données réelles (pourquoi ce plafond a
  été ajouté, pourquoi certaines notes comme "B" restent difficiles à
  atteindre).

```powershell
python -m judge.agent_judge <chemin_json_anomalies>
```

### `reporting/` — rapports HTML

- **`agent_reporting.py`** : `generer_rapport_fichier()` (une page HTML par
  fichier : score, verdict, tableau des anomalies triées par sévérité) et
  `generer_rapport_consolide()` (vue d'ensemble sur tous les fichiers d'un
  run : répartition des verdicts, anomalies par type/sévérité, top des
  anomalies les plus fréquentes, évolution par mois). Tout texte affiché
  est échappé (protection contre l'injection HTML, les justifications
  provenant en bout de chaîne d'un LLM lisant des données externes).
- **`regles_reporting.md`** : format attendu des données en entrée,
  notion de "même anomalie" pour le calcul de fréquence.

```powershell
python -m reporting.agent_reporting fichier <chemin_verdict.json>
python -m reporting.agent_reporting consolide [dossier_resultats]
```

### `ingestion/` — accès aux fichiers de données

- **`local_loader.py`** (`LocalLoader`) : lit les fichiers depuis `DATA_DIR`
  local (développement).
- **`s3_client.py`** (`S3Loader`) : même interface publique que
  `LocalLoader`, mais télécharge depuis S3 avec cache local — utilisé au
  déploiement AWS uniquement.
- **`test_s3_connexion.py`** : diagnostic de connexion S3 avant déploiement.
- **`profiling.py`** : génère `resultats/inventaire.csv`, un inventaire de
  tous les fichiers de `DATA_DIR` (colonnes, taille, langue probable) à
  titre purement informatif — jamais utilisé pour unifier les schémas.

```powershell
python -m ingestion.profiling
python -m ingestion.test_s3_connexion
```

### `docs/`

- **`contrat_anomalie.md`** : le format JSON commun décrit plus haut, avec
  le détail des énumérations et l'usage du score de confiance.

### `tests/`

Un fichier de test par module (`test_structure.py`, `test_stats.py`,
`test_agent_realisme.py`, `test_agent_judge.py`, `test_agent_reporting.py`,
`test_orchestrateur.py`), plus des tests de conformité au contrat commun.
Aucun test ne fait d'appel réseau réel : les tests LLM utilisent un client
factice (voir `_ClientFactice` dans `test_agent_realisme.py` et
`test_orchestrateur.py`) pour ne jamais consommer le quota gratuit partagé.

```powershell
python -m pytest tests/ -v
```

### Dossiers non versionnés (exclus par `.gitignore`)

- **`data/`** : fichiers Excel/CSV réels à auditer, organisés par mois
  (`LOAD_LOCK_202603/`, `LOAD_LOCK_202604/`...). Volumineux et
  potentiellement confidentiel : jamais commité, à récupérer séparément
  (voir « Installation » ci-dessous).
- **`resultats/`** : toutes les sorties générées (`structure_*.json`,
  `stats_*.json`, `realisme_*.json`, `verdict_*.json`, `rapport_*.html`,
  `inventaire.csv`) — régénérées à chaque exécution, jamais commitées.
- **`cache_llm/`** : cache disque des réponses LLM (évite de repayer une
  requête identique) et compteur de quota quotidien
  (`compteur_quotidien.json`). État d'exécution local, jamais commité.

## Installation pour un nouveau membre

1. Cloner le dépôt et se placer sur son propre travail (branche personnelle,
   ex. `git checkout -b <prenom>`).

2. Créer un environnement virtuel Python 3.12 et l'activer :

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Installer les dépendances :

   ```powershell
   pip install -r requirements.txt
   ```

4. Créer son propre `.env` à partir du modèle, puis renseigner **sa propre**
   clé `OPENROUTER_API_KEY` (personnelle, à ne jamais commiter) :

   ```powershell
   copy .env.example .env
   ```

5. Récupérer le dossier `data/` séparément (non versionné) : demander aux
   autres membres l'emplacement de partage actuel et le placer à la racine
   du projet.

6. Vérifier que tout fonctionne :

   ```powershell
   python -m pytest tests/ -v
   ```

## Contraintes importantes à connaître avant de lancer un run réel

- **Quota LLM partagé** : `config.RATE_LIMIT["max_requetes_jour"] = 45`,
  partagé par tout le projet (Réalisme, et à terme Cohérence/Judge). Un
  appel LLM peut prendre de 30 secondes à plusieurs minutes sur le tier
  gratuit. `agents.llm_client.quota_restant()` donne le solde du jour ;
  `orchestrateur.main()` s'arrête proprement quand il atteint 0.
- **Fichiers de même nom dans des mois différents** : plusieurs fichiers
  `data/LOAD_LOCK_*/` portent le même nom (ex. `NL_B2B_BRO_OFF.xlsx` existe
  dans 3 dossiers mensuels). `orchestrateur.py` traite chaque copie
  séparément et range ses résultats par mois — mais si vous testez un
  module individuellement (`python -m structure.agent_structure <nom>`),
  la résolution par nom seul renvoie systématiquement la **première**
  copie trouvée : donnez un chemin complet si vous visez une copie précise.

## État actuel du pipeline

| Étape | Statut |
|---|---|
| Structure | ✅ Implémenté, testé, calibré sur données réelles |
| Stats | ✅ Implémenté, testé, calibré sur données réelles |
| Cohérence | ❌ Pas encore dans ce dépôt (en cours côté binôme) |
| Réalisme | ✅ Implémenté, testé, calibré |
| Judge | ✅ Implémenté, testé, calibré |
| Reporting | ✅ Implémenté (HTML), testé |
| Orchestrateur | ✅ Implémenté, testé — tourne sans Cohérence pour l'instant |
| Dépôt S3 des rapports (`config.S3_REPORTS_PREFIX`) | ❌ Pas encore implémenté |
