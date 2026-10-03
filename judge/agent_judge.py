"""Judge déterministe : normalisation, consolidation, score /100 et blocking.

Le score pénalise des lignes, pas des signalements. Voir regles_judge.md.
"""

import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

LIBELLES_VERDICT = {"ACCEPTED": "accepté", "WARNING": "accepté sous réserve", "REJECTED": "rejeté"}
ORDRE_SEVERITE = {"low": 0, "medium": 1, "high": 2, "critical": 3}
REGLES_BLOQUANTES = {"fichier_vide", "fichier_illisible", "structure_inexploitable"}
ALIASES_TYPE = {"stats": "statistique", "realism": "realisme"}


def _texte(valeur) -> str:
    return "" if valeur is None else str(valeur).strip()


def _canonique(valeur) -> str:
    texte = unicodedata.normalize("NFKD", _texte(valeur).casefold())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", texte))


def _entier(valeur):
    if isinstance(valeur, bool):
        return None
    try:
        nombre = float(valeur)
        return int(nombre) if math.isfinite(nombre) and nombre >= 0 and nombre.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _confiance(valeur) -> float:
    try:
        nombre = float(valeur)
        if isinstance(valeur, bool) or not math.isfinite(nombre):
            return 1.0
        return max(0.0, min(1.0, nombre))
    except (TypeError, ValueError, OverflowError):
        return 1.0


def _normaliser_anomalie(nom_fichier: str, brut: dict) -> dict:
    """Préserve les extensions et complète le contrat historique sans le muter."""
    anomalie = dict(brut)
    type_ = _texte(brut.get("type")).casefold()
    type_ = ALIASES_TYPE.get(type_, type_)
    type_ = type_ if type_ in config.TYPES_ANOMALIE else "inconnu"
    severite = _texte(brut.get("severite", brut.get("severity"))).casefold()
    colonnes = brut.get("colonnes") or []
    if isinstance(colonnes, str):
        colonnes = [colonnes]
    elif not isinstance(colonnes, (list, tuple, set)):
        colonnes = []
    source = _texte(brut.get("source")).casefold()
    if type_ in {"coherence", "realisme"}:
        source = "llm"
    source = source or ("stats" if type_ in {"structure", "statistique"} else "inconnue")
    sources = brut.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, (list, tuple, set)):
        sources = []
    ligne = _entier(brut.get("ligne"))
    lignes = brut.get("lignes")
    lignes = lignes if isinstance(lignes, (list, tuple, set)) else []
    lignes = sorted({idx for item in lignes if (idx := _entier(item)) is not None})
    portee = _texte(brut.get("portee")).casefold()
    if portee not in {"ligne", "colonne", "fichier"}:
        portee = "ligne" if ligne is not None or lignes else "inconnue"
    nb_affectees = _entier(brut.get("nb_lignes_affectees"))
    regle = _canonique(brut.get("regle", brut.get("rule_id"))).replace(" ", "_")
    justification = _texte(brut.get("justification")) or "Anomalie sans justification fournie."

    # Reconnaissance ciblée des messages structurels réellement émis aujourd'hui.
    if type_ == "structure" and source == "stats" and not brut.get("portee"):
        diagnostic = _canonique(justification)
        if diagnostic == "le fichier ne contient aucune ligne ou aucune colonne exploitable":
            regle, portee = "fichier_vide", "fichier"
        elif "est entierement vide 100 de valeurs manquantes" in diagnostic:
            portee = "colonne"
        elif "valeurs manquantes" in diagnostic:
            compte = re.search(r"\((\d+)/(\d+)\)", _texte(brut.get("valeur")))
            if compte:
                nb_affectees, portee = int(compte.group(1)), "colonne"
    # Les agrégats historiques fournissent parfois le compte, mais pas tous les index.
    compte = re.match(r"^(\d+) (?:valeur\(s\)|ligne\(s\))", _texte(brut.get("valeur")))
    if compte and type_ in {"structure", "statistique"} and not lignes:
        nb_affectees = nb_affectees if nb_affectees is not None else int(compte.group(1))

    anomalie.update(
        fichier=_texte(brut.get("fichier")) or nom_fichier,
        ligne=ligne, lignes=lignes, colonnes=sorted({_texte(c) for c in colonnes if _texte(c)}),
        type=type_, severite=severite if severite in ORDRE_SEVERITE else "medium",
        source=source, sources=sorted({_texte(s) for s in sources if _texte(s)} | {type_ if type_ != "inconnu" else source}),
        valeur=_texte(brut.get("valeur")), justification=justification,
        confiance=_confiance(brut.get("confiance", brut.get("confidence"))),
        portee=portee, nb_lignes_affectees=nb_affectees, regle=regle,
        probleme_id=_canonique(brut.get("probleme_id", brut.get("problem_id"))),
        blocking=(type_ == "structure" and source == "stats" and regle in REGLES_BLOQUANTES),
    )
    return anomalie


def _meme_probleme(a: dict, b: dict) -> bool:
    """Ne fusionne jamais sur la seule ligne ou la seule sévérité."""
    if any(a[k] != b[k] for k in ("fichier", "ligne", "lignes", "portee")):
        return False
    ca, cb = set(a["colonnes"]), set(b["colonnes"])
    if ca != cb and (not ca or not cb or len(ca & cb) / len(ca | cb) < config.JUDGE_SIMILARITE_COLONNES):
        return False
    if a["regle"] or b["regle"]:
        return bool(a["regle"] and a["regle"] == b["regle"])
    if a["probleme_id"] or b["probleme_id"]:
        return bool(a["probleme_id"] and a["probleme_id"] == b["probleme_id"])
    ja, jb = _canonique(a["justification"]), _canonique(b["justification"])
    if ja == jb and ja != _canonique("Anomalie sans justification fournie."):
        return _canonique(a["valeur"]) == _canonique(b["valeur"]) or not a["valeur"] or not b["valeur"]
    ta, tb = set(ja.split()), set(jb.split())
    def sensibles(mots):
        return {mot for mot in mots if any(c.isdigit() for c in mot) or mot in {"pas", "non", "sans", "avant", "apres", "haut", "bas"}}
    return bool(a["valeur"] and _canonique(a["valeur"]) == _canonique(b["valeur"])
                and min(len(ta), len(tb)) >= 5 and sensibles(ta) == sensibles(tb)
                and len(ta & tb) / len(ta | tb) >= config.JUDGE_SIMILARITE_PROBLEME)


def _consolider(anomalies: list) -> list:
    groupes = defaultdict(list)
    for anomalie in anomalies:
        cle = (anomalie["fichier"], anomalie["ligne"], tuple(anomalie["lignes"]), anomalie["portee"])
        groupes[cle].append(anomalie)
    resultat = []
    for groupe in groupes.values():
        clusters = []
        for anomalie in sorted(groupe, key=lambda a: json.dumps(a, sort_keys=True, ensure_ascii=False, default=str)):
            # Accord avec tous les membres : évite une fusion transitive abusive.
            cluster = next((c for c in clusters if all(_meme_probleme(anomalie, membre) for membre in c)), None)
            if cluster is None:
                clusters.append([anomalie])
            else:
                cluster.append(anomalie)
        for cluster in clusters:
            principale = sorted(cluster, key=lambda a: (-ORDRE_SEVERITE[a["severite"]], -a["confiance"], a["type"], a["justification"]))[0]
            fusion = dict(principale)
            fusion["sources"] = sorted({s for a in cluster for s in a["sources"]})
            fusion["colonnes"] = sorted({c for a in cluster for c in a["colonnes"]})
            fusion["confiance"] = max(a["confiance"] for a in cluster)
            fusion["blocking"] = any(a["blocking"] for a in cluster)
            fusion["nb_signalements"] = len(cluster)
            comptes = [a["nb_lignes_affectees"] for a in cluster if a["nb_lignes_affectees"] is not None]
            fusion["nb_lignes_affectees"] = max(comptes) if comptes else None
            fusion["justifications"] = sorted({a["justification"] for a in cluster})
            resultat.append(fusion)
    return sorted(resultat, key=lambda a: (a["fichier"], a["ligne"] if a["ligne"] is not None else -1, a["type"], tuple(a["colonnes"]), a["justification"]))


def _couverture(anomalies: list, nb_lignes_total: int | None):
    """Poids maximal par ligne ; agrégats sans index = borne basse de l'union."""
    poids_lignes, agregats = {}, []
    estimation = False
    for a in anomalies:
        poids = config.JUDGE_POIDS_SEVERITE[a["severite"]]
        if a["source"] == "llm":
            poids *= a["confiance"]
        indices = set(a["lignes"])
        if a["portee"] == "ligne" and a["ligne"] is not None:
            indices.add(a["ligne"])
        if nb_lignes_total is not None:
            valides = {i for i in indices if i < nb_lignes_total}
            estimation |= valides != indices
            indices = valides
        for idx in indices:
            poids_lignes[idx] = max(poids_lignes.get(idx, 0.0), poids)
        compte = a["nb_lignes_affectees"]
        if a["portee"] in {"colonne", "fichier"} and compte is None:
            compte = nb_lignes_total
        if compte is not None:
            compte = max(len(indices), compte)
            if nb_lignes_total is not None:
                estimation |= compte > nb_lignes_total
                compte = min(compte, nb_lignes_total)
            if compte > len(indices):
                agregats.append((poids, compte))
                estimation |= compte != nb_lignes_total
        elif not indices:
            agregats.append((poids, 1 if nb_lignes_total != 0 else 0))
            estimation = True
    lignes_affectees = max([len(poids_lignes)] + [n for _, n in agregats])
    denominateur = nb_lignes_total if nb_lignes_total is not None else max(1, lignes_affectees)
    # Intégration par niveaux : O(N log N), sans construire des lignes fictives.
    histogramme = Counter(poids_lignes.values())
    anonymes = defaultdict(int)
    for poids, compte in agregats:
        anonymes[poids] = max(anonymes[poids], compte)
    niveaux = sorted(set(histogramme) | set(anonymes), reverse=True)
    penalite, connus, anonymes_cumules = 0.0, 0, 0
    for i, niveau in enumerate(niveaux):
        connus += histogramme[niveau]
        anonymes_cumules = max(anonymes_cumules, anonymes[niveau])
        suivant = niveaux[i + 1] if i + 1 < len(niveaux) else 0.0
        penalite += (niveau - suivant) * max(connus, anonymes_cumules)
    taux = lignes_affectees / denominateur if denominateur else 0.0
    penalite = penalite / denominateur if denominateur else 0.0
    return penalite, lignes_affectees, taux, estimation


def _calculer_score(anomalies: list, nb_lignes_total: int | None = None) -> float:
    penalite, _, _, _ = _couverture(anomalies, nb_lignes_total)
    return max(0.0, min(100.0, 100.0 * (1.0 - penalite)))


def _grade_from_score(score: float) -> str:
    for grade, seuil in sorted(config.JUDGE_SEUILS_GRADE.items(), key=lambda item: item[1], reverse=True):
        if score >= seuil:
            return grade
    return "F"


def _verdict_from_score(score: float, blocking: bool = False) -> str:
    if blocking or score < config.JUDGE_SEUIL_WARNING:
        return "REJECTED"
    return "ACCEPTED" if score >= config.JUDGE_SEUIL_ACCEPTED else "WARNING"


def _compter_par_champ(anomalies: list, champ: str, valeurs_possibles: tuple) -> dict:
    compteur = dict.fromkeys(valeurs_possibles, 0)
    for anomalie in anomalies:
        valeur = anomalie[champ]
        compteur[valeur] = compteur.get(valeur, 0) + 1
    return compteur


def _generer_justification(verdict: str, anomalies: list) -> str:
    libelle = LIBELLES_VERDICT[verdict]
    if not anomalies:
        return f"Fichier {libelle} : aucune anomalie détectée."
    exemple = max(anomalies, key=lambda a: (ORDRE_SEVERITE[a["severite"]], a["confiance"]))
    return (f"Fichier {libelle} : {len(anomalies)} anomalie(s) unique(s). "
            f"Exemple le plus sévère (ligne {exemple['ligne']}, colonne(s) {', '.join(exemple['colonnes']) or '-'}) : "
            f"{exemple['justification']}")


def analyser(
    nom_fichier: str,
    anomalies: list,
    nb_lignes_total: int | None = None,
    pipeline_status: str = "COMPLETE",
    pipeline_reason: str | None = None,
) -> dict:
    """L'ancien appel à deux arguments reste valide, avec score provisoire si N manque."""
    pipeline_status = _texte(pipeline_status).upper() or "COMPLETE"
    if pipeline_status not in {"COMPLETE", "INCOMPLETE"}:
        raise ValueError("pipeline_status doit valoir COMPLETE ou INCOMPLETE")
    if nb_lignes_total is not None:
        n = _entier(nb_lignes_total)
        if n is None:
            raise ValueError("nb_lignes_total doit être un entier positif ou nul")
        nb_lignes_total = n
    if anomalies is None:
        brutes = []
    elif isinstance(anomalies, (list, tuple)):
        brutes = list(anomalies)
    else:
        brutes = [anomalies]
    normalisees = [_normaliser_anomalie(nom_fichier, a) for a in brutes if isinstance(a, dict)]
    uniques = _consolider(normalisees)
    penalite, nb_affectees, taux, estimation = _couverture(uniques, nb_lignes_total)
    score_calcule = round(max(0.0, min(100.0, 100.0 * (1.0 - penalite))), 4)
    raisons = sorted({a["justification"] for a in uniques if a["blocking"]})
    blocking = bool(raisons)
    verdict = _verdict_from_score(score_calcule, blocking)
    provisoire = nb_lignes_total is None and bool(brutes)
    invalides = len(brutes) - len(normalisees)
    # Un périmètre inconnu ou une entrée non interprétable ne justifie pas ACCEPTED.
    if (provisoire or invalides) and verdict == "ACCEPTED":
        verdict = "WARNING"
    if pipeline_status == "INCOMPLETE":
        score = None
        grade = None
        verdict = None
        justification = "Analyse incomplète : aucun verdict qualité ni score n'est produit."
        if pipeline_reason:
            justification += f" Motif : {pipeline_reason}"
    else:
        score = score_calcule
        grade = _grade_from_score(score)
        justification = _generer_justification(verdict, uniques)
        if blocking:
            justification += " Règle bloquante : " + " ; ".join(raisons)
        if provisoire:
            justification += " Score provisoire : nombre total de lignes non fourni."
        if estimation:
            justification += " Couverture estimée : index incomplets ou périmètre invalide."
        if invalides:
            justification += f" {invalides} signalement(s) non interprétable(s)."
    return {
        "fichier": nom_fichier, "score": score, "score_echelle": 100,
        "grade": grade, "verdict": verdict,
        "pipeline_status": pipeline_status, "pipeline_reason": pipeline_reason,
        "blocking": blocking, "blocking_reasons": raisons,
        "nb_anomalies_total": len(uniques), "anomalies_total_brutes": len(brutes),
        "anomalies_uniques": len(uniques), "anomalies_invalides": invalides,
        "anomalies_par_type": _compter_par_champ(uniques, "type", config.TYPES_ANOMALIE),
        "anomalies_par_severite": _compter_par_champ(uniques, "severite", tuple(ORDRE_SEVERITE)),
        "nb_lignes_total": nb_lignes_total, "lignes_affectees": nb_affectees,
        "taux_lignes_affectees": round(taux, 6), "couverture_estimee": estimation,
        "score_provisoire": provisoire, "penalite": None if pipeline_status == "INCOMPLETE" else round(100.0 * penalite, 4),
        "anomalies_consolidees": uniques, "anomalies": uniques,
        "justification": justification,
    }


def main() -> None:
    if len(sys.argv) not in {2, 3}:
        print("Usage : python -m judge.agent_judge <json_anomalies> [nb_lignes_total]")
        sys.exit(1)
    chemin = Path(sys.argv[1])
    contenu = json.loads(chemin.read_text(encoding="utf-8"))
    if isinstance(contenu, dict):
        anomalies = contenu.get("anomalies", contenu.get("anomalies_consolidees", []))
        nom_fichier = contenu.get("fichier") or chemin.stem
        nb_lignes = contenu.get("nb_lignes_total", contenu.get("nb_lignes_totales"))
        pipeline_status = contenu.get("pipeline_status", "COMPLETE")
        pipeline_reason = contenu.get("pipeline_reason")
    else:
        anomalies = contenu
        nom_fichier = next((a.get("fichier") for a in anomalies if isinstance(a, dict) and a.get("fichier")), chemin.stem)
        nb_lignes = None
        pipeline_status = "COMPLETE"
        pipeline_reason = None
    if len(sys.argv) == 3:
        nb_lignes = sys.argv[2]
    verdict = analyser(
        nom_fichier,
        anomalies,
        nb_lignes_total=nb_lignes,
        pipeline_status=pipeline_status,
        pipeline_reason=pipeline_reason,
    )
    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    sortie = config.RESULTATS_DIR / f"verdict_{chemin.stem}.json"
    sortie.write_text(json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Verdict sauvegardé dans {sortie}")


if __name__ == "__main__":
    main()
