# Contrat de format des anomalies

Ce document définit le format JSON commun utilisé par **toutes** les couches
du pipeline (validation de structure, détection statistique, agents LLM,
judge) pour représenter une anomalie détectée dans un fichier de données.

Toute anomalie produite par n'importe quel composant du pipeline doit
respecter ce schéma, afin que le module de reporting puisse les agréger
sans traitement spécifique par source.

## Schéma

```json
{
  "fichier": "NL_B2B_ABT_OFF.xlsx",
  "ligne": 42,
  "colonnes": ["date_debut", "date_fin"],
  "type": "coherence",
  "severite": "high",
  "source": "llm",
  "valeur": "date_fin < date_debut",
  "justification": "La date de fin du contrat est antérieure à sa date de début.",
  "confiance": 0.92
}
```

## Champs

| Champ           | Type            | Description                                                                 |
|-----------------|-----------------|-------------------------------------------------------------------------------|
| `fichier`       | `string`        | Nom du fichier source dans lequel l'anomalie a été détectée.                 |
| `ligne`         | `int`           | Index pandas d'origine, **0-based**, lorsqu'une ligne précise a été conservée. L'ancien contrat utilisait aussi `0` pour certaines anomalies globales ; cette ambiguïté est signalée comme telle dans la traçabilité et ne doit jamais être présentée comme une cellule exacte. |
| `colonnes`      | `list[string]`  | Colonne(s) impliquée(s) dans l'anomalie.                                     |
| `type`          | `enum`          | Catégorie de l'anomalie. Valeurs possibles : `structure`, `statistique`, `coherence`, `realisme`. |
| `severite`      | `enum`          | Gravité de l'anomalie. Valeurs possibles : `critical`, `high`, `medium`, `low`. |
| `source`        | `enum`          | Origine de la détection. Valeurs possibles : `stats`, `llm`.                 |
| `valeur`        | `string`        | Représentation textuelle de la valeur (ou combinaison de valeurs) posant problème. |
| `justification` | `string`        | Explication en langage naturel de la raison pour laquelle l'anomalie est signalée. |
| `confiance`     | `float`         | Score de confiance de la détection, compris entre `0.0` et `1.0`.            |

## Détail des énumérations

### `type`

- `structure` : non-conformité par rapport au schéma attendu (colonne manquante, type incorrect, format invalide, etc.). Détecté par la couche de validation de structure (pandas).
- `statistique` : valeur aberrante ou incohérence détectée par des méthodes statistiques (outliers, distributions anormales, doublons, etc.).
- `coherence` : incohérence logique ou métier entre plusieurs champs/lignes, détectée par l'agent LLM « Cohérence ».
- `realisme` : valeur individuellement invraisemblable au regard du métier du renting automobile, détectée par l'agent LLM « Réalisme ».

### `severite`

- `critical` : bloquant, empêche toute exploitation fiable de la donnée.
- `high` : erreur importante, à corriger avant utilisation.
- `medium` : anomalie notable mais n'empêchant pas l'exploitation immédiate.
- `low` : anomalie mineure ou cosmétique.

Ces niveaux sont pondérés dans `config.py` (`PONDERATION_SEVERITE`) pour
calculer un score agrégé par fichier ou par lot.

### `source`

- `stats` : anomalie détectée par les couches pandas / statistiques (déterministe, reproductible).
- `llm` : anomalie détectée par un agent LLM (Cohérence, Réalisme) ou confirmée par le Judge.

## Utilisation du score de confiance

Le champ `confiance` (entre 0 et 1) permet au module Judge d'agréger les
anomalies remontées par les différentes sources en un verdict global par
fichier, selon les seuils définis dans `config.py` :

- `ACCEPTED` si le score agrégé est `>= 0.90`
- `WARNING` si le score agrégé est `>= 0.75` (et `< 0.90`)
- `REJECTED` en dessous de `0.75`

Le détail exact du calcul d'agrégation (moyenne pondérée, minimum, etc.)
sera défini lors de l'implémentation du module `judge/`.

## Enrichissement de traçabilité persisté

Après le Judge, l'orchestrateur ajoute un bloc purement descriptif sans
modifier la détection, la sévérité, la confiance, le score ou le verdict :

- `anomaly_id`, `run_id`, `audit_timestamp_utc`, `load_lock`, `filename`,
  `source_reference` ;
- `agent` / `agents`, `issue_type`, `description`, `detected_value` ;
- `location.scope` : `CELL`, `ROW`, `COLUMN`, `FILE` ou `MULTI_ROW` ;
- `location.row_index_0_based` et `location.row_indices_0_based` uniquement
  quand les indices exacts sont disponibles ;
- `location.row_number_1_based` = index + 1 pour l'affichage métier ;
- `location.physical_row_number_1_based` = index + 2 pour les CSV/Excel lus
  avec une ligne d'en-tête par défaut ;
- `location.available`, `precision` et `unavailable_reason` afin de ne jamais
  inventer une ligne, une colonne, une cellule ou un onglet Excel absent.

Le loader conserve désormais un sidecar technique (jamais ajouté au
DataFrame ni envoyé aux agents) avec le nom réel de la première feuille,
l'ordre des colonnes, le nombre de lignes d'en-tête et la preuve que l'index
est un `RangeIndex` 0-based. Pour un nouveau fichier Excel chargé par cette
chaîne, une anomalie CELL exacte peut donc recevoir une référence comme
`S670`. Sans ce sidecar (notamment pour les artefacts legacy), la ligne
physique et la cellule restent `null`.
Les anomalies multi-lignes ne publient une liste exacte que si les indices
ont réellement été persistés ; un simple compte et une ligne représentative
produisent une localisation `PARTIAL`.
