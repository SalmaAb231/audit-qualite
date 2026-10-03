# Règles de l'agent Reporting

Ce document décrit `reporting/agent_reporting.py`, dernier maillon du
pipeline : il ne détecte ni ne recalcule rien, il met en forme en HTML ce
que `judge/agent_judge.py` a déjà produit. 100% déterministe, aucun appel
LLM — pure présentation.

## Deux rapports, deux fonctions

- `generer_rapport_fichier(verdict, anomalies)` : un fichier, une page HTML
  (score, verdict, grade, justification, tableau des anomalies triées par
  sévérité — critical en premier).
- `generer_rapport_consolide(fichiers_verdicts)` : vue d'ensemble sur tous
  les fichiers traités (répartition des verdicts, anomalies par type/par
  sévérité sommées, top anomalies les plus fréquentes, évolution par mois,
  tableau des fichiers du plus problématique au meilleur).

## Forme attendue de `fichiers_verdicts`

La signature demandée est `generer_rapport_consolide(fichiers_verdicts: list[dict])`
— une liste de verdicts, sans paramètre séparé pour les anomalies brutes.
Or "top anomalies les plus fréquentes" a besoin du détail par anomalie
(colonnes, justification), que le verdict seul (`judge.agent_judge.analyser()`)
ne contient pas — il ne contient que des compteurs agrégés
(`anomalies_par_type`, `anomalies_par_severite`).

Décision : chaque dict de `fichiers_verdicts` peut porter deux clés
optionnelles, au-delà du contrat de `analyser()` :

- `"anomalies"` (`list[dict]`) : anomalies brutes de ce fichier. Absente →
  ce fichier ne contribue pas au top des anomalies fréquentes, sans erreur
  (section explicitement signalée comme incomplète plutôt que silencieuse).
- `"dossier_mois"` (`str`, ex. `"LOAD_LOCK_202603"`) : nécessaire pour
  "Évolution par mois". Absente sur tous les fichiers → section explicite
  "aucune information de mois disponible", jamais un tableau vide muet.

`orchestrateur.py` ajoute actuellement `"anomalies"` et `"dossier_mois"` à
chaque verdict avant la sauvegarde et avant la construction du rapport
consolidé. La CLI autonome du Judge ajoute elle aussi le détail des anomalies,
mais ne peut connaître le mois d'origine que si cette information figure dans
son entrée.

## Analyses incomplètes

Un verdict portant `pipeline_status="INCOMPLETE"` est rendu avec un badge
« Analyse incomplète ». Son score et son grade `null` sont affichés avec `-`,
jamais avec zéro. Le rapport consolidé possède un compteur `INCOMPLETE`, les
inclut dans l'évolution mensuelle et les place visiblement dans le tableau des
fichiers. Reporting ne tente pas de recalculer un verdict à partir des
anomalies partielles.

## "Top anomalies les plus fréquentes" : quelle notion de "même anomalie" ?

Deux anomalies sont considérées identiques si elles ont le même `type`, les
mêmes `colonnes` et la même `justification` (`_signature_anomalie`). Choix
motivé par un cas réel observé pendant le développement de `agents/agent_realisme.py` :
plusieurs lignes d'un même fichier peuvent porter l'exact même diagnostic
("Volvo n'a pas de modèle EX40...") — regrouper ces répétitions identifie un
problème systémique (un modèle mal orthographié partout) plutôt que de le
diluer en N anomalies isolées dans le rapport consolidé.

## Sécurité : tout texte dynamique est échappé

`justification`, `valeur`, noms de fichiers et de colonnes proviennent en
bout de chaîne d'agents LLM lisant des fichiers Excel externes : aucun de
ces textes n'est digne de confiance par défaut. Tout est passé par
`html.escape()` avant insertion dans le HTML généré, pour éviter qu'une
valeur de cellule contenant `<script>` ou similaire ne s'exécute dans le
navigateur d'un expert qui ouvre le rapport.

## Ce que ce module ne fait pas

Il ne sauvegarde ni n'envoie rien vers S3 (`config.S3_REPORTS_PREFIX`) : il
retourne des chaînes HTML, l'écriture sur disque (CLI) ou l'upload S3 (à
écrire) sont des responsabilités séparées. Il ne valide pas non plus le
contrat des anomalies reçues (déjà fait par chaque détecteur via
`agents/parse_utils.valider_anomalies` ou l'équivalent structure/stats) —
Reporting fait confiance à ce qu'on lui donne, dans la limite de l'échappement
HTML décrit ci-dessus.
