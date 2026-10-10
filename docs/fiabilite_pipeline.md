# Fiabilité, statuts d'exécution et reprise du pipeline

Ce document décrit les garanties ajoutées au pipeline pour qu'une panne LLM,
un quota épuisé ou une réponse JSON invalide ne puisse pas être confondu avec
un fichier sans anomalie. Il complète les règles métier propres à chaque
agent.

## Principe fondamental

Une liste d'anomalies vide ne signifie « aucune anomalie détectée » que si
l'étape correspondante a terminé avec le statut `SUCCESS`. Les agents LLM
renvoient donc séparément :

- leurs anomalies validées ;
- leur statut d'exécution (`SUCCESS`, `PARTIAL` ou `FAILED`) ;
- le nombre de chunks réussis et échoués ;
- le nombre de lignes analysées et non analysées ;
- une erreur exploitable lorsqu'une partie du traitement a échoué.

Le statut global vaut :

| Situation | Statut de l'agent |
|---|---|
| Tous les chunks ont réussi | `SUCCESS` |
| Au moins un chunk a réussi et au moins un a échoué | `PARTIAL` |
| Aucun chunk n'a réussi | `FAILED` |

Si Cohérence ou Réalisme n'est pas en `SUCCESS`, l'orchestrateur marque le
fichier `pipeline_status="INCOMPLETE"`. Le Judge conserve les anomalies déjà
trouvées pour le diagnostic, mais renvoie `score=null`, `grade=null` et
`verdict=null`. Un échec technique ne peut donc plus produire un faux verdict
`ACCEPTED`.

## Checkpoints de l'agent Cohérence

Cohérence est l'étape la plus coûteuse, car elle analyse normalement tout le
fichier. Chaque chunk terminé est enregistré dans
`resultats/coherence/checkpoints/`.

L'identité d'un checkpoint est calculée ainsi :

```text
SHA256(source_path + "|" + content_hash + "|" + checkpoint_version)
```

- `source_path` est le chemin source relatif stable, pas seulement le nom du
  fichier. Deux fichiers de même nom dans deux dossiers mensuels ne partagent
  donc pas leur checkpoint.
- `content_hash` est le SHA-256 du contenu réel du fichier. Une modification
  des données invalide automatiquement l'ancien checkpoint.
- `checkpoint_version` vaut actuellement `coherence-v1`. Elle doit être
  incrémentée manuellement lorsqu'un changement de prompt, de modèle ou de
  découpage rend les anciennes réponses incompatibles.

Le JSON de checkpoint conserve aussi ces trois informations en clair afin de
faciliter le diagnostic.

En mode S3, `source_path` est la clé de l'objet et le hash concerne les octets
du fichier téléchargé dans le cache local. Avant réutilisation, le chargeur
compare bucket, clé, ETag, VersionId, LastModified et taille avec son sidecar
`.s3-cache.json`, puis vérifie le SHA-256 local. Un remplacement de même taille
déclenche donc un téléchargement si l'identité distante change. Le GET utilise
`IfMatch` et la réponse doit correspondre à l'identité observée, VersionId inclus.
Il lit l'objet courant sans demander une ancienne version : la permission
existante `s3:GetObject` suffit, sans imposer `s3:GetObjectVersion`.
Une différence entre HEAD et GET ou un téléchargement incomplet fait échouer
le chargement ; l'ancien cache n'est pas analysé comme solution de repli.
Un cache ancien sans sidecar valide est téléchargé à nouveau. Les checkpoints
restent locaux ; ils ne sont pas écrits dans S3.

À la reprise :

- un chunk enregistré avec `metadata.succes=true` est réutilisé sans nouvel
  appel LLM ;
- un chunk absent ou enregistré en échec est rappelé ;
- un checkpoint illisible ou corrompu est ignoré et l'analyse redémarre
  proprement ;
- l'écriture actuelle n'est pas atomique : une interruption exactement au
  moment de l'écriture peut donc imposer de retraiter le fichier.

Le checkpoint reprend Cohérence à l'intérieur d'un fichier. Il n'existe pas
encore de checkpoint global permettant de reprendre automatiquement le run au
« fichier N » : relancer l'orchestrateur reparcourt l'inventaire, mais les
chunks Cohérence déjà réussis sont récupérés.

## Réponses LLM, retries et cache

`agents/llm_client.py` applique les protections suivantes :

1. recherche d'une réponse identique dans `cache_llm/` ;
2. appel du premier modèle configuré pour l'agent ;
3. au plus un retry court par modèle pour certaines erreurs réseau, serveur
   ou de réponse/JSON, avec `FREE_TIER_RETRY_BACKOFF_SECONDS` ;
4. passage immédiat au modèle suivant pour les erreurs permanentes ou les
   limites upstream ; un circuit breaker écarte un modèle après deux limites
   upstream pendant 300 secondes ;
5. dernier recours avec `MODELE_FALLBACK` si le quota et le budget le permettent.

Toutes les tentatives réseau et changements de modèle partagent un plafond
de `MAX_LLM_ATTEMPTS_PER_CHUNK=4`. La cascade et le fallback ne sont donc pas
nécessairement parcourus entièrement. Réalisme utilise seulement le budget
restant du chunk pour sa vérification web. Les anciennes valeurs
`RATE_LIMIT["retries_max"]` / `backoff_initial` ne pilotent pas ce mécanisme actuel.

Chaque tentative réseau réelle consomme le compteur quotidien. Une réponse
issue du cache n'en consomme aucune. Les avertissements « JSON invalide » ou
« nouvel essai » ne sont pas des échecs définitifs : il faut regarder le
statut final de l'étape.

## Sélection et analyse Réalisme

L'orchestrateur construit une seule fois le lot Réalisme : lignes signalées
par Structure, Stats ou Cohérence, complétées par un échantillon de contrôle.
Il appelle ensuite `analyser_avec_statut(..., echantillonner=False)` afin
qu'aucune ligne explicitement suspecte ne disparaisse lors d'un second tirage.

L'appel autonome historique `analyser()` reste disponible et retourne
seulement la liste d'anomalies. Pour piloter un traitement fiable ou obtenir
les compteurs d'exécution, utiliser `analyser_avec_statut()`.

La vérification web est volontairement « best effort » : si DuckDuckGo ou
l'appel LLM de vérification échoue, l'anomalie candidate est conservée. Cette
vérification ne rend donc pas le pipeline incomplet ; seul l'échec de l'analyse
LLM principale du chunk affecte son statut.

## Quota et propagation vers le Judge

`main()` vérifie le quota avant chaque fichier. `pipeline_fichier()` le
revérifie avant Réalisme, après l'étape Cohérence qui peut avoir consommé les
dernières requêtes. Si le quota vaut zéro, Réalisme n'est pas appelé et reçoit
un résultat explicite `FAILED`.

Le Judge est entièrement déterministe et ne consomme aucune requête LLM. En
entrée, il reçoit `pipeline_status` et `pipeline_reason` :

- `COMPLETE` : calcul normal du score sur 100, du grade et du verdict ;
- `INCOMPLETE` : aucun score ni verdict qualité, mais conservation des
  anomalies et compteurs disponibles.

## Reporting des traitements incomplets

Les rapports HTML individuels et consolidés comprennent un état
`INCOMPLETE` (« Analyse incomplète »). Les valeurs absentes sont affichées
avec `-` et non converties en zéro. Les fichiers incomplets restent visibles
dans la répartition globale, l'évolution mensuelle et le tableau des fichiers.

## Vérification et diagnostic

Les tests automatisés n'appellent ni OpenRouter, ni DuckDuckGo, ni S3. Les
clients et accès externes sont simulés.

```powershell
python -m pytest -q
python -m orchestrateur
Get-Content .\audit-qualite.log -Wait -Tail 30
Get-ChildItem .\resultats -Recurse
```

Pour considérer un fichier complètement audité, vérifier simultanément :

- `pipeline_status == "COMPLETE"` dans `verdict_*.json` ;
- `etapes_pipeline.coherence.status == "SUCCESS"` ;
- `etapes_pipeline.realisme.status == "SUCCESS"` ;
- `nb_lignes_echec == 0` pour les deux étapes LLM.

