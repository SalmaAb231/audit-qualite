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
| `ligne`         | `int`           | Numéro de la ligne concernée (index 0-based ou 1-based, à fixer et documenter lors de l'implémentation). |
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
