# Règles du Judge déterministe (v2)

## Pourquoi le score change

L'ancien score sur [0, 1] additionnait les signalements sans connaître la
taille du fichier. Une seule high retirait 0,7 et une critical saturait le
score à zéro. Un doublon entre détecteurs pesait plusieurs fois. Ce score
confondait gravité, fréquence et exploitabilité.

Le Judge reste sans LLM, sans accès réseau et sans horloge : mêmes entrées
et même configuration donnent les mêmes résultats, même si l'ordre des
anomalies change. Il ne modifie pas les anomalies reçues.

## Entrée et normalisation

Signature : analyser(nom_fichier, anomalies, nb_lignes_total=None).

L'orchestrateur transmet maintenant len(df), y compris pour le rejet
structurel. Le Judge conserve les champs historiques et ajoute les champs
de consolidation. Les variantes severity/confidence, stats/realism sont
acceptées. Une colonne string devient une liste ; les index numériques
valides deviennent des entiers ; une sévérité inconnue devient medium.
La confiance absente/invalide vaut 1,0 ; elle est bornée à [0, 1].
Le type inconnu est conservé sous la catégorie explicite inconnu.
Un signalement non objet est compté dans anomalies_invalides.

Extensions facultatives :
- lignes : liste exacte des index affectés (0-based) ;
- portee : ligne, colonne ou fichier ;
- nb_lignes_affectees : compte agrégé quand tous les index ne sont pas disponibles ;
- probleme_id (ou problem_id) : identifiant stable d'un problème ;
- regle (ou rule_id) : identifiant stable d'une règle déterministe.

L'index 0 est une vraie ligne tant qu'aucune portée explicite ne dit autrement.
Les formats historiques connus sont reconnus de manière ciblée :
fichier vide, colonne entièrement vide, nombre de valeurs manquantes dans
"(179/180)", et comptes "36 valeur(s)" ou "3 ligne(s)".
Aucun nombre total de lignes n'est inventé à partir du dernier index.

## Déduplication et consolidation

Les candidats doivent concerner le même fichier, la même ligne/liste
d'index et la même portée. Leurs colonnes doivent être identiques, incluses
l'une dans l'autre, partager une colonne principale établie, ou avoir une
similarité de Jaccard d'au moins JUDGE_SIMILARITE_COLONNES (0,8).

Le problème doit ensuite correspondre :
1. même identifiant de règle (`regle` / `rule_id`), si présent des deux côtés ;
2. sinon même `probleme_id` / `problem_id` ;
3. sinon même `problem_family` ;
4. sinon justification identique après normalisation des accents/casse/
   ponctuation, avec valeurs compatibles ;
5. sinon même valeur et justifications très proches (Jaccard >= 0,85,
   au moins cinq mots), sans différence de nombres ni de mots sensibles
   tels que non, pas, avant/apres, haut/bas.

Tous les membres d'un groupe doivent être compatibles : une chaîne de
ressemblances ne suffit pas. La seule ligne, les seules colonnes ou la
seule sévérité ne permettent jamais de fusionner. Pas de compréhension
sémantique par LLM : les paraphrases très différentes restent distinctes
si aucun identifiant commun ni élément concordant sur la colonne et la
valeur du problème n'est fourni.

Une fusion conserve la sévérité maximale, la confiance maximale, l'union
des colonnes et des sources (noms des détecteurs), toutes les justifications
et nb_signalements. La justification principale est choisie de façon stable
(sévérité, confiance, type, texte). Les compteurs par type attribuent le
problème une seule fois au type principal ; sources conserve la provenance
complète. Aucun changement des agents n'est nécessaire.

### Consolidation inter-agents

Les colonnes sont également compatibles si un ensemble non vide est inclus
dans l'autre, ou si les deux signalements partagent une colonne principale
explicite (`colonne_principale` / `main_column`). Sans colonne principale
explicite, une valeur identique attribuée à une colonne commune et des
diagnostics concordants peuvent établir cette compatibilité.

Une identité structurée commune est prioritaire sur le texte. Tout conflit
entre identifiants renseignés des deux côtés interdit la fusion, même si la
famille ou le texte est identique. Un identifiant renseigné d'un seul côté
ne suffit pas : en l'absence d'identifiant commun, ces signalements restent
séparés.

Pour les anciens signalements sans identifiant, le repli inter-agents exige
la même valeur dans une colonne commune (valeur simple ou affectation
`Colonne=valeur`) et au moins deux mots descriptifs communs, hors nombres,
unités, noms de colonnes et mots génériques. Les diagnostics opposés
haut/bas, supérieur/inférieur et avant/après restent séparés. Ce repli est
lexical et conservateur : il ne refait aucune analyse métier et ne garantit
pas la fusion de toutes les paraphrases possibles.

L'inclusion des colonnes ou la seule ligne ne prouvent jamais le même
problème. Chaque nouveau membre doit correspondre à **tous** les membres du
groupe ; l'union des colonnes ne sert pas de pont pour fusionner deux
problèmes incompatibles. `signalements_originaux` contient une copie
indépendante de chaque signalement reçu pour la traçabilité.

Le verdict distingue `nb_signalements_bruts`, `nb_problemes_consolides` et
`lignes_affectees`. La justification emploie « problèmes consolidés ».
Les champs historiques `anomalies_total_brutes`, `anomalies_uniques` et
`nb_anomalies_total` sont conservés comme alias pour les consommateurs
existants ; les deux derniers comptent les problèmes après consolidation,
sans prétendre à une déduplication sémantique exhaustive. Les rapports
existants reçoivent cette liste consolidée et ces compteurs sans modification
du module reporting.

## Score global progressif sur 100

Configuration spécifique JUDGE_* dans config.py ; les poids historiques
PONDERATION_SEVERITE restent disponibles pour le score interne de Cohérence.

| Sévérité | Poids |
|---|---:|
| critical | 1,0 |
| high | 0,6 |
| medium | 0,25 |
| low | 0,1 |

Pour une anomalie LLM : poids_effectif = poids_severite × confiance.
Pour une règle déterministe : poids_effectif = poids_severite.

Pour chaque ligne, on retient le **maximum** des poids effectifs des
problèmes qui la touchent. Deux problèmes distincts sont bien conservés,
mais leur addition ne multiplie pas artificiellement le taux de lignes
touchées. Le risque de la ligne est représenté par son problème le plus grave.

Pour N > 0 :
    penalite_points = 100 × somme(poids_maximal_par_ligne) / N
    score = max(0, min(100, 100 − penalite_points))

Avec des lignes distinctes et confiance 1 :
    score = 100 × (1 − taux_critical − 0,6 × taux_high
                      − 0,25 × taux_medium − 0,1 × taux_low)

Une critical isolée sur dix lignes donne 90 ; sur 100 000 lignes,
99,999. Une high sur dix lignes donne 94. Des critical couvrant toutes
les lignes peuvent encore donner zéro : c'est la proportion touchée,
pas la présence d'un seul signalement, qui le provoque.

### Comptes agrégés et informations absentes

Sans tous les index, on ne peut pas connaître l'intersection de deux
populations. À chaque niveau de poids, le Judge retient le maximum entre
le nombre d'index connus et les comptes agrégés de ce niveau ou plus graves.
Cela donne une **borne basse** des lignes affectées et de la pénalité :
on suppose le recouvrement maximal au lieu d'additionner aveuglément.
Exemple : 30 high et 50 medium sans index, sur 100 lignes :
30 × 0,6 + 20 × 0,25 = 23 points ; score 77.
couverture_estimee=true signale cette limite. Le score peut être optimiste :
ce n'est pas une mesure exacte de couverture.

Une portée colonne/fichier sans compte signifie N lignes ; un signalement
sans index ni portée identifiable représente au moins une ligne estimée.
Les index hors limites sont exclus et les comptes limités à N, avec
couverture_estimee=true. Une liste d'index exacte évite ces approximations.

L'ancien appel à deux arguments reste utilisable. Si N est absent, le
dénominateur de repli est le nombre de lignes affectées connues/estimées
(au moins 1), et score_provisoire=true dès qu'il existe des signalements.
nb_lignes_total reste null. Un tel résultat ne peut pas être ACCEPTED :
il est au minimum WARNING. Les signalements non objets imposent aussi
au minimum WARNING. Ces réserves apparaissent dans justification.

Pour N=0, aucune pénalité de ligne n'est calculable : score 100. Un fichier
vide identifié reste REJECTED par la règle bloquante indépendante du score.
Pas de sous-scores par dimension : le maximum par ligne permet une formule
globale simple sans compter deux fois un problème commun à plusieurs canaux.

## Blocking indépendant de la sévérité

Seules les règles déterministes de type structure et source stats figurant
dans la liste fermée sont bloquantes :
- fichier_vide ;
- fichier_illisible ;
- structure_inexploitable.

Le message exact du détecteur Structure actuel pour un fichier vide est
reconnu sans nécessiter une modification de ce détecteur. Pour les autres
cas, il faut fournir regle/rule_id. Un blocking=true isolé, une critical
ou un texte libre ne suffit pas : aucune règle indispensable au schéma
n'est inventée. Le Judge recalcule blocking à partir de ces critères.
Les anomalies Cohérence/Réalisme sont normalisées source=llm et ne sont
jamais automatiquement bloquantes, même si elles demandent blocking=true.

Le pipeline ignore toujours les fichiers dont l'ingestion retourne None ;
il n'émet pas encore une anomalie fichier_illisible pour eux. Le Judge sait
traiter cette règle si elle lui est fournie ; il ne change pas l'ingestion.

## Verdict et grade

Avant d'appliquer les règles de qualité ci-dessous, le Judge vérifie
`pipeline_status`. Si sa valeur est `INCOMPLETE`, au moins une étape LLM
obligatoire n'a pas terminé : le résultat contient alors `score=null`,
`grade=null`, `verdict=null` et `penalite=null`. Les anomalies déjà détectées,
les compteurs et `pipeline_reason` sont conservés pour le diagnostic. Cette
branche empêche un échec technique ou un quota épuisé de devenir un verdict
`ACCEPTED` fondé sur une liste d'anomalies artificiellement vide.

Les règles suivantes s'appliquent uniquement à un pipeline `COMPLETE`.

| Condition | Verdict |
|---|---|
| blocking=true | REJECTED, indépendamment du score |
| score < 70 | REJECTED |
| 70 <= score < 90 | WARNING |
| score >= 90 | ACCEPTED |

Seuils configurables : JUDGE_SEUIL_WARNING et JUDGE_SEUIL_ACCEPTED.
Les réserves des entrées incomplètes sont décrites plus haut.

Grades : A >= 90 ; B >= 80 ; C >= 70 ; D >= 60 ; sinon F.
Le grade exprime le score ; il peut rester A avec un verdict REJECTED
pour une règle bloquante. Les bornes s'appliquent au score publié,
arrondi à quatre décimales.

## Sortie et compatibilité

Les champs historiques fichier, score, grade, verdict, justification,
nb_anomalies_total, anomalies_par_type et anomalies_par_severite demeurent.
score est désormais sur 100 (score_echelle=100) ; nb_anomalies_total et les
compteurs représentent les problèmes uniques.

Champs ajoutés : blocking, blocking_reasons, anomalies_total_brutes,
anomalies_uniques, anomalies_invalides, anomalies_consolidees,
nb_lignes_total, lignes_affectees, taux_lignes_affectees, penalite,
score_provisoire et couverture_estimee.
taux_lignes_affectees est une fraction [0, 1] ; penalite est en points /100.
anomalies est l'alias compatible de anomalies_consolidees. L'orchestrateur
ne le remplace plus par les listes brutes. Le reporting utilise le détail
consolidé et marque les nouveaux scores /100 ; les anciens JSON restent lisibles.
Les anciens rapports ne sont ni migrés ni réécrits par cette refonte.

CLI :
    python -m judge.agent_judge chemin.json [nb_lignes_total]

Le nombre peut aussi être fourni dans nb_lignes_total/nb_lignes_totales
du JSON. Le Judge seul peut ainsi être recalculé sans rappeler les LLM.

## Exemple : 180 lignes

Supposons 14 lignes distinctes, aucun blocking et confiance 1 :
2 critical, 4 high, 3 medium, 5 low.

Lignes affectées = 14 ; taux = 14/180 = 7,7778 %.
Somme des poids = 2×1 + 4×0,6 + 3×0,25 + 5×0,1 = 5,65.
Pénalité = 100×5,65/180 = 3,1389 points.
Score = 96,8611 ; grade A ; verdict ACCEPTED.

Le nombre de problèmes ne suffit pas si les lignes se recouvrent :
la localisation et la confiance sont nécessaires pour refaire le calcul.

## Validation

tests/test_agent_judge.py couvre les dix cas demandés, la normalisation,
les limites de confiance, les portées agrégées, les seuils, la stabilité
par permutation et la compatibilité des rapports. Les tests orchestrateur
vérifient la transmission de N et l'usage des anomalies consolidées.
Aucun test de cette refonte n'effectue d'appel LLM/web réel.
