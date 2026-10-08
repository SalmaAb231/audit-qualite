"""
Agent Reporting du pipeline audit-qualite.

Dernier maillon du pipeline : transforme les verdicts de judge/agent_judge.py
et les anomalies des détecteurs en rapports HTML lisibles par des experts non
techniques — un rapport par fichier, et un rapport consolidé sur l'ensemble
des fichiers traités. Ne détecte ni ne recalcule rien : pure mise en forme,
100% déterministe, aucun appel LLM.

Convention pour generer_rapport_consolide() : chaque dict de fichiers_verdicts
est un verdict de judge.agent_judge.analyser(), avec deux clés optionnelles
que l'appelant (l'orchestrateur, à terme) peut ajouter :
- "anomalies" (list[dict]) : problèmes de ce fichier, utilisés pour
  le "top anomalies les plus fréquentes". Absente -> ce fichier ne contribue
  pas au calcul (pas d'erreur, juste moins de détail).
- "dossier_mois" (str, ex. "LOAD_LOCK_202603") : utilisée pour "Évolution par
  mois". Absente sur tous les fichiers -> section explicitement signalée
  comme indisponible plutôt que silencieuse ("si dates dispo").
judge/agent_judge.py enrichit déjà ses fichiers verdict_*.json sauvegardés
avec la clé "anomalies" (voir sa CLI) : la CLI 'consolide' ci-dessous les lit
directement.

Tout texte dynamique (noms de fichiers, justifications, valeurs...) est
échappé (module html) avant insertion dans le HTML : ces textes proviennent
de détecteurs LLM sur des données externes, jamais dignes de confiance par
défaut.

Usage :
    python -m reporting.agent_reporting fichier <chemin_verdict_enrichi.json>
    python -m reporting.agent_reporting consolide [dossier_resultats]
"""

import html
import json
import sys
from collections import Counter
from pathlib import Path

from loguru import logger

# Permet l'exécution directe du script en plus de l'exécution en tant que module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ORDRE_SEVERITE = {"critical": 0, "high": 1, "medium": 2, "low": 3}
NB_TOP_ANOMALIES = 10

LIBELLES_VERDICT = {
    "ACCEPTED": "Accepté",
    "WARNING": "Accepté sous réserve",
    "REJECTED": "Rejeté",
    "INCOMPLETE": "Analyse incomplète",
}

STYLE_COMMUN = """
body { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; margin: 2rem auto; max-width: 1100px; color: #1a1a2e; background: #fafafa; line-height: 1.5; }
h1 { margin-bottom: 0.25rem; }
h2 { margin-top: 2rem; border-bottom: 2px solid #e0e0e0; padding-bottom: 0.4rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
th, td { border: 1px solid #e5e5e5; padding: 0.5rem 0.75rem; text-align: left; font-size: 0.9rem; vertical-align: top; }
th { background: #f0f1f5; font-weight: 600; }
tr.sev-critical { background: #fdecea; }
tr.sev-high { background: #fef3e6; }
tr.sev-medium { background: #fef9e7; }
tr.sev-low { background: #eef7ee; }
.badge { display: inline-block; padding: 0.25rem 0.65rem; border-radius: 999px; font-weight: 600; color: white; font-size: 0.8rem; }
.badge-ACCEPTED { background: #2e7d32; }
.badge-WARNING { background: #ef8a00; }
.badge-REJECTED { background: #c62828; }
.badge-INCOMPLETE { background: #616161; }
.summary-grid { display: flex; gap: 1.25rem; margin: 1.25rem 0; flex-wrap: wrap; }
.summary-card { background: white; border: 1px solid #e5e5e5; border-radius: 8px; padding: 1rem 1.5rem; min-width: 140px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.summary-card .valeur { font-size: 1.8rem; font-weight: 700; }
.summary-card .libelle { font-size: 0.8rem; color: #666; text-transform: uppercase; letter-spacing: 0.03em; }
.muted { color: #777; font-size: 0.9rem; }
"""


# ---------------------------------------------------------------------------
# Petits constructeurs HTML communs
# ---------------------------------------------------------------------------


def _badge(texte: str, classe: str) -> str:
    return f'<span class="badge badge-{html.escape(classe)}">{html.escape(texte)}</span>'


def _carte_resume(valeur, libelle: str) -> str:
    return (
        f'<div class="summary-card"><div class="valeur">{html.escape(str(valeur))}</div>'
        f'<div class="libelle">{html.escape(libelle)}</div></div>'
    )


def _page_html(titre: str, corps: str) -> str:
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        f"<title>{html.escape(titre)}</title><style>{STYLE_COMMUN}</style></head>"
        f"<body>{corps}</body></html>"
    )


# ---------------------------------------------------------------------------
# Rapport par fichier
# ---------------------------------------------------------------------------


def _trier_par_severite(anomalies: list) -> list:
    return sorted(anomalies, key=lambda a: ORDRE_SEVERITE.get(a.get("severite"), 99))


def _sources(anomalie: dict) -> list:
    sources = anomalie.get("sources") or [anomalie.get("type") or anomalie.get("source") or "inconnue"]
    if isinstance(sources, str):
        sources = [sources]
    return sorted(set(str(s) for s in sources))


def _problemes(verdict: dict) -> list:
    return verdict.get("anomalies_consolidees", verdict.get("anomalies")) or []


def _nb_problemes(verdict: dict, anomalies: list | None = None):
    return verdict.get("nb_problemes_consolides", verdict.get("nb_anomalies_total", len(_problemes(verdict) if anomalies is None else anomalies)))


def _signalements_bruts(verdict: dict):
    """La provenance originale seule permet de répartir un problème multi-agents."""
    if "anomalies_consolidees" not in verdict and "anomalies" not in verdict:
        return None
    signalements = []
    for a in _problemes(verdict):
        originaux = a.get("signalements_originaux")
        if originaux:
            signalements.extend(originaux)
        elif a.get("nb_signalements", 1) == 1 and len(_sources(a)) == 1:
            signalements.append(a)
        else:
            return None
    return signalements


def _nb_bruts(verdict: dict):
    if "nb_signalements_bruts" in verdict:
        return verdict["nb_signalements_bruts"]
    if "anomalies_total_brutes" in verdict:
        return verdict["anomalies_total_brutes"]
    bruts = _signalements_bruts(verdict)
    return len(bruts) if bruts is not None else "-"


def _ligne_tableau_anomalie(anomalie: dict) -> str:
    severite = anomalie.get("severite") or ""
    colonnes = ", ".join(anomalie.get("colonnes") or []) or "-"
    location = anomalie.get("location") if isinstance(anomalie.get("location"), dict) else {}
    scope = location.get("scope") or anomalie.get("portee") or "-"
    if location.get("available") and location.get("row_index_0_based") is not None:
        ligne = (
            f"index {location['row_index_0_based']} (data row {location.get('row_number_1_based')}; "
            f"physical row {location.get('physical_row_number_1_based') or '-'})"
        )
    elif location:
        ligne = "Unavailable" if not location.get("available") else scope
    else:
        ligne = str(anomalie.get("ligne", "-"))
    return (
        f'<tr class="sev-{html.escape(severite)}">'
        f"<td>{html.escape(str(scope))}</td>"
        f"<td>{html.escape(ligne)}</td>"
        f"<td>{html.escape(colonnes)}</td>"
        f"<td>{html.escape(anomalie.get('type') or '')}</td>"
        f"<td>{html.escape(', '.join(_sources(anomalie)))}</td>"
        f"<td>{html.escape(str(anomalie.get('nb_signalements', 1)))}</td>"
        f"<td>{html.escape(severite)}</td>"
        f"<td>{html.escape(str(anomalie.get('valeur', '')))}</td>"
        f"<td>{html.escape(anomalie.get('justification') or '')}</td>"
        f"<td>{html.escape(str(anomalie.get('confiance', '-')))}</td>"
        "</tr>"
    )


def _tableau_anomalies(anomalies: list) -> str:
    if not anomalies:
        return '<p class="muted">Aucune anomalie détectée.</p>'
    lignes = "".join(_ligne_tableau_anomalie(a) for a in _trier_par_severite(anomalies))
    return (
        "<table><thead><tr><th>Portée</th><th>Ligne source</th><th>Colonne(s)</th><th>Type principal</th>"
        "<th>Sources</th><th>Signalements</th>"
        "<th>Sévérité</th><th>Valeur</th><th>Justification</th><th>Confiance</th>"
        f"</tr></thead><tbody>{lignes}</tbody></table>"
    )


def generer_rapport_fichier(verdict: dict, anomalies: list) -> str:
    """
    Rapport HTML pour un seul fichier : score, verdict, grade, justification
    du Judge, et le détail de ses anomalies triées par sévérité (critical en
    premier). Ne lève jamais d'exception sur des champs manquants (valeurs
    de repli affichées à la place).
    """
    anomalies = verdict.get("anomalies_consolidees", anomalies) or []
    fichier = verdict.get("fichier", "")
    verdict_code = (
        "INCOMPLETE" if verdict.get("pipeline_status") == "INCOMPLETE"
        else verdict.get("verdict", "")
    )

    corps = (
        f"<h1>Rapport qualité — {html.escape(fichier)}</h1>"
        f"<p>{_badge(LIBELLES_VERDICT.get(verdict_code, verdict_code), verdict_code)}</p>"
        '<div class="summary-grid">'
        + _carte_resume(
            verdict.get("score") if verdict.get("score") is not None else "-",
            "Score /100" if verdict.get("score_echelle") == 100 else "Score",
        )
        + _carte_resume(verdict.get("grade") if verdict.get("grade") is not None else "-", "Grade")
        + _carte_resume(_nb_bruts(verdict), "Signalements bruts")
        + _carte_resume(_nb_problemes(verdict, anomalies), "Problèmes consolidés")
        + _carte_resume(verdict.get("lignes_affectees", "-"), "Lignes affectées")
        + "</div>"
        f"<p>{html.escape(verdict.get('justification') or '')}</p>"
        + (f"<p>Couverture estimée : {'oui' if verdict['couverture_estimee'] else 'non'}.</p>" if "couverture_estimee" in verdict else "")
        + "<h2>Détail des problèmes consolidés</h2>"
        + _tableau_anomalies(anomalies)
    )
    return _page_html(f"Rapport — {fichier}", corps)


# ---------------------------------------------------------------------------
# Rapport consolidé
# ---------------------------------------------------------------------------


def _compter_verdicts(fichiers_verdicts: list) -> dict:
    compteur = Counter(
        "INCOMPLETE" if fv.get("pipeline_status") == "INCOMPLETE" else fv.get("verdict")
        for fv in fichiers_verdicts
    )
    return {v: compteur.get(v, 0) for v in ("ACCEPTED", "WARNING", "REJECTED", "INCOMPLETE")}


def _sommer_compteurs(fichiers_verdicts: list, champ: str, cles: tuple) -> dict:
    total = dict.fromkeys(cles, 0)
    for fv in fichiers_verdicts:
        for cle, valeur in (fv.get(champ) or {}).items():
            if cle in total:
                total[cle] += valeur
    return total


def _signature_anomalie(anomalie: dict) -> tuple:
    """Regroupe les anomalies identiques (même type/colonnes/justification), potentiellement répétées sur plusieurs fichiers/lignes."""
    return (anomalie.get("type") or "", tuple(anomalie.get("colonnes") or []), anomalie.get("justification") or "")


def _top_anomalies_frequentes(fichiers_verdicts: list, n: int = NB_TOP_ANOMALIES) -> list:
    compteur = Counter()
    for fv in fichiers_verdicts:
        for anomalie in fv.get("anomalies_consolidees", fv.get("anomalies")) or []:
            compteur[_signature_anomalie(anomalie)] += 1
    return compteur.most_common(n)


def _evolution_par_mois(fichiers_verdicts: list) -> dict:
    """dict mois -> {"ACCEPTED": n, "WARNING": n, "REJECTED": n}, trié par mois. {} si aucun fichier n'a de 'dossier_mois'."""
    par_mois = {}
    for fv in fichiers_verdicts:
        mois = fv.get("dossier_mois")
        if not mois:
            continue
        compteur = par_mois.setdefault(
            mois, {"ACCEPTED": 0, "WARNING": 0, "REJECTED": 0, "INCOMPLETE": 0}
        )
        verdict_code = "INCOMPLETE" if fv.get("pipeline_status") == "INCOMPLETE" else fv.get("verdict")
        if verdict_code in compteur:
            compteur[verdict_code] += 1
    return dict(sorted(par_mois.items()))


def _section_repartition_verdicts(fichiers_verdicts: list) -> str:
    compteur = _compter_verdicts(fichiers_verdicts)
    cartes = "".join(
        _carte_resume(compteur[v], LIBELLES_VERDICT[v])
        for v in ("ACCEPTED", "WARNING", "REJECTED", "INCOMPLETE")
    )
    return f'<h2>Répartition des verdicts ({len(fichiers_verdicts)} fichier(s))</h2><div class="summary-grid">{cartes}</div>'


def _section_repartition(titre: str, compteur: dict) -> str:
    cartes = "".join(_carte_resume(n, cle) for cle, n in compteur.items())
    return f'<h2>{html.escape(titre)}</h2><div class="summary-grid">{cartes}</div>'


def _section_signalements_bruts(fichiers_verdicts: list) -> str:
    compteur = Counter(dict.fromkeys(config.TYPES_ANOMALIE, 0))
    manquants = []
    for fv in fichiers_verdicts:
        bruts = _signalements_bruts(fv)
        if bruts is None:
            manquants.append(fv)
            continue
        for a in bruts:
            agent = a.get("type") or "inconnu"
            agent = {"stats": "statistique", "realism": "realisme"}.get(agent, agent)
            compteur[agent] += 1
    if manquants:
        # Ne présente jamais les types principaux comme des signalements bruts.
        return (
            '<h2>Signalements bruts par agent</h2><p class="muted">'
            'Répartition complète indisponible : signalements originaux non fournis.</p>'
            + _section_repartition(
                "Type principal des problèmes consolidés",
                _sommer_compteurs(fichiers_verdicts, "anomalies_par_type", config.TYPES_ANOMALIE),
            )
        )
    return _section_repartition("Signalements bruts par agent", dict(compteur)) + _carte_resume(sum(compteur.values()), "Total des signalements bruts")


def _section_problemes(fichiers_verdicts: list) -> str:
    details_disponibles = all("anomalies_consolidees" in fv or "anomalies" in fv for fv in fichiers_verdicts)
    multi = sum(len(_sources(a)) > 1 for fv in fichiers_verdicts for a in _problemes(fv)) if details_disponibles else "-"
    return (
        '<h2>Problèmes consolidés</h2><div class="summary-grid">'
        + _carte_resume(sum(_nb_problemes(fv) for fv in fichiers_verdicts), "Total des problèmes consolidés")
        + _carte_resume(multi, "Problèmes confirmés par plusieurs agents")
        + '</div>'
    )


def _section_top_anomalies(fichiers_verdicts: list) -> str:
    top = _top_anomalies_frequentes(fichiers_verdicts)
    if not top:
        return (
            "<h2>Top anomalies les plus fréquentes</h2>"
            '<p class="muted">Détail par anomalie non fourni pour ce rapport (clé "anomalies" absente).</p>'
        )
    lignes = "".join(
        f"<tr><td>{html.escape(type_)}</td><td>{html.escape(', '.join(colonnes) or '-')}</td>"
        f"<td>{html.escape(justification)}</td><td>{n}</td></tr>"
        for (type_, colonnes, justification), n in top
    )
    return (
        "<h2>Top anomalies les plus fréquentes</h2>"
        "<table><thead><tr><th>Type principal</th><th>Colonne(s)</th><th>Justification</th><th>Occurrences</th></tr></thead>"
        f"<tbody>{lignes}</tbody></table>"
    )


def _section_evolution_mensuelle(fichiers_verdicts: list) -> str:
    evolution = _evolution_par_mois(fichiers_verdicts)
    if not evolution:
        return (
            "<h2>Évolution par mois</h2>"
            '<p class="muted">Aucune information de mois disponible pour ces fichiers (clé "dossier_mois" absente).</p>'
        )
    lignes = "".join(
        f"<tr><td>{html.escape(mois)}</td><td>{c['ACCEPTED']}</td><td>{c['WARNING']}</td>"
        f"<td>{c['REJECTED']}</td><td>{c['INCOMPLETE']}</td></tr>"
        for mois, c in evolution.items()
    )
    return (
        "<h2>Évolution par mois</h2>"
        "<table><thead><tr><th>Mois</th><th>Accepté</th><th>Accepté sous réserve</th>"
        "<th>Rejeté</th><th>Analyse incomplète</th></tr></thead>"
        f"<tbody>{lignes}</tbody></table>"
    )


def _section_fichiers(fichiers_verdicts: list) -> str:
    tries = sorted(
        fichiers_verdicts,
        key=lambda fv: (
            0 if fv.get("pipeline_status") == "INCOMPLETE" else 1,
            fv.get("score") if fv.get("score") is not None else float("inf"),
        ),
    )
    lignes = "".join(
        f"<tr><td>{html.escape(fv.get('fichier') or '')}</td>"
        f"<td>{_badge('Analyse incomplète', 'INCOMPLETE') if fv.get('pipeline_status') == 'INCOMPLETE' else _badge(fv.get('verdict') or '', fv.get('verdict') or '')}</td>"
        f"<td>{html.escape(str(fv.get('grade') if fv.get('grade') is not None else '-'))}</td>"
        f"<td>{html.escape(str(fv.get('score') if fv.get('score') is not None else '-'))}</td>"
        f"<td>{html.escape(str(_nb_bruts(fv)))}</td>"
        f"<td>{html.escape(str(_nb_problemes(fv)))}</td>"
        f"<td>{html.escape(str(fv.get('lignes_affectees', '-')))}</td></tr>"
        for fv in tries
    )
    return (
        "<h2>Détail par fichier (du plus problématique au meilleur)</h2>"
        "<table><thead><tr><th>Fichier</th><th>Verdict</th><th>Grade</th><th>Score</th>"
        "<th>Signalements bruts</th><th>Problèmes consolidés</th><th>Lignes affectées</th></tr></thead>"
        f"<tbody>{lignes}</tbody></table>"
    )


def generer_rapport_consolide(fichiers_verdicts: list) -> str:
    """
    Rapport HTML consolidé sur l'ensemble des fichiers traités : répartition
    des verdicts, signalements bruts par agent et problèmes consolidés par sévérité (sommés sur tous les
    fichiers), top anomalies les plus fréquentes et évolution par mois (les
    deux seulement "si dispo", voir docstring du module), et tableau de tous
    les fichiers triés du plus problématique au meilleur.
    """
    fichiers_verdicts = fichiers_verdicts or []
    corps = (
        "<h1>Rapport consolidé — audit qualité</h1>"
        + _section_repartition_verdicts(fichiers_verdicts)
        + _section_signalements_bruts(fichiers_verdicts)
        + _section_problemes(fichiers_verdicts)
        + _section_repartition(
            "Problèmes consolidés par sévérité",
            _sommer_compteurs(fichiers_verdicts, "anomalies_par_severite", tuple(config.PONDERATION_SEVERITE.keys())),
        )
        + _section_top_anomalies(fichiers_verdicts)
        + _section_evolution_mensuelle(fichiers_verdicts)
        + _section_fichiers(fichiers_verdicts)
    )
    return _page_html("Rapport consolidé — audit qualité", corps)


# ---------------------------------------------------------------------------
# Point d'entrée CLI
# ---------------------------------------------------------------------------


def _generer_rapport_fichier_cli(chemin: Path) -> None:
    contenu = json.loads(chemin.read_text(encoding="utf-8"))
    anomalies = contenu.pop("anomalies", []) if isinstance(contenu, dict) else []
    rapport = generer_rapport_fichier(contenu, anomalies)

    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    fichier_sortie = config.RESULTATS_DIR / f"rapport_{chemin.stem}.html"
    fichier_sortie.write_text(rapport, encoding="utf-8")
    logger.info(f"Rapport sauvegardé dans {fichier_sortie}")


def _generer_rapport_consolide_cli(dossier: Path) -> None:
    fichiers_verdicts = [
        json.loads(chemin.read_text(encoding="utf-8")) for chemin in sorted(dossier.glob("verdict_*.json"))
    ]
    if not fichiers_verdicts:
        logger.error(f"Aucun verdict_*.json trouvé dans {dossier}")
        sys.exit(1)

    rapport = generer_rapport_consolide(fichiers_verdicts)
    config.RESULTATS_DIR.mkdir(parents=True, exist_ok=True)
    fichier_sortie = config.RESULTATS_DIR / "rapport_consolide.html"
    fichier_sortie.write_text(rapport, encoding="utf-8")
    logger.info(f"{len(fichiers_verdicts)} fichier(s) agrégé(s). Rapport sauvegardé dans {fichier_sortie}")


def main() -> None:
    usage = (
        "Usage : python -m reporting.agent_reporting fichier <chemin_verdict_enrichi.json>\n"
        "        python -m reporting.agent_reporting consolide [dossier_resultats]"
    )
    if len(sys.argv) < 2 or sys.argv[1] not in ("fichier", "consolide"):
        print(usage)
        sys.exit(1)

    if sys.argv[1] == "fichier":
        if len(sys.argv) != 3:
            print(usage)
            sys.exit(1)
        _generer_rapport_fichier_cli(Path(sys.argv[2]))
    else:
        dossier = Path(sys.argv[2]) if len(sys.argv) == 3 else config.RESULTATS_DIR
        _generer_rapport_consolide_cli(dossier)


if __name__ == "__main__":
    main()
