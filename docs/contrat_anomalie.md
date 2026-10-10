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

- `structure` : problème de forme (fichier vide, noms de colonnes absents/dupliqués, valeurs manquantes, mélange de types, etc.), détecté par pandas sans liste métier de colonnes obligatoires.
- `statistique` : valeur aberrante ou incohérence détectée par des méthodes statistiques (outliers, distributions anormales, doublons, etc.).
- `coherence` : incohérence logique ou métier entre plusieurs champs/lignes, détectée par l'agent LLM « Cohérence ».
- `realisme` : valeur individuellement invraisemblable au regard du métier du renting automobile, détectée par l'agent LLM « Réalisme ».

### `severite`

- `critical` : gravité maximale. Le garde Structure arrête les étapes suivantes pour une critical structurelle ; dans le Judge v2, la sévérité seule ne constitue pas une règle bloquante.
- `high` : erreur importante, à corriger avant utilisation.
- `medium` : anomalie notable mais n'empêchant pas l'exploitation immédiate.
- `low` : anomalie mineure ou cosmétique.

Le Judge v2 utilise `config.JUDGE_POIDS_SEVERITE` (critical 1, high 0,6,
medium 0,25, low 0,1). `PONDERATION_SEVERITE` concerne encore le score auxiliaire
historique de Cohérence ; ce n'est pas le score final du Judge.

### `source`

- `stats` : anomalie détectée par les couches pandas / statistiques (déterministe, reproductible).
- `llm` : anomalie détectée par un agent LLM (Cohérence, Réalisme). Le Judge est déterministe et ne fait pas de confirmation LLM.

## Utilisation du score de confiance

Le champ `confiance` (entre 0 et 1) permet au module Judge d'agréger les
anomalies remontées par les différentes sources en un verdict global par
fichier, selon les seuils définis dans `config.py` :

- `ACCEPTED` si le score sur 100 est `>= 90`, sans blocage ;
- `WARNING` si `70 <= score < 90`, sans blocage ;
- `REJECTED` si le score est `< 70` ou si une règle structurelle bloquante est reconnue.

La confiance pondère les signalements LLM ; le Judge retient le poids maximal
par ligne après consolidation des problèmes concordants. Les portées et comptes
agrégés sans index exacts sont traités comme une couverture estimée.
Lorsque le pipeline est `INCOMPLETE`, score, grade et verdict qualité restent
à `null`. Voir [les règles du Judge v2](../judge/regles_judge.md) pour la formule,
les cas provisoires et les règles bloquantes indépendantes de la sévérité.

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
