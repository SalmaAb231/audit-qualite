"""Tests de l'interface d'audit avec mocks uniquement."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard.audit_service import (
    AuditServiceError,
    group_files_by_load_lock,
    list_available_files,
    run_full_load_lock,
    run_selected_files,
    run_single_file,
)


def _runner_recorder(appels):
    def runner(**kwargs):
        appels.append(kwargs)
        return {"run_id": "run-dashboard", "verdicts": []}

    return runner


def test_single_file_transmet_nom_et_load_lock_une_seule_fois():
    appels = []
    resultat = run_single_file("a.xlsx", "LOAD_LOCK_202603", runner=_runner_recorder(appels))
    assert resultat["run_id"] == "run-dashboard"
    assert appels == [{"noms_fichiers": ["a.xlsx"], "dossier_mois": "LOAD_LOCK_202603"}]


def test_multiple_files_transmet_exactement_la_selection_dans_un_run():
    appels = []
    selection = ["a.xlsx", "b.csv"]
    run_selected_files(selection, "LOAD_LOCK_202603", runner=_runner_recorder(appels))
    assert appels == [{"noms_fichiers": selection, "dossier_mois": "LOAD_LOCK_202603"}]


def test_full_load_lock_transmet_le_dossier_sans_liste_de_noms():
    appels = []
    run_full_load_lock("LOAD_LOCK_202603", runner=_runner_recorder(appels))
    assert appels == [{"noms_fichiers": None, "dossier_mois": "LOAD_LOCK_202603"}]


def test_erreur_orchestrateur_est_encapsulee_proprement():
    def runner(**kwargs):
        raise RuntimeError("secret technique simulé")

    with pytest.raises(AuditServiceError, match="Consultez les logs"):
        run_single_file("a.xlsx", "LOAD_LOCK_202603", runner=runner)


def test_listing_input_et_groupement_load_lock_sans_boto_reel():
    class _LoaderFactice:
        def lister_fichiers(self):
            return [
                SimpleNamespace(
                    nom="a.xlsx",
                    dossier_mois="LOAD_LOCK_202603",
                    chemin="input/LOAD_LOCK_202603/a.xlsx",
                    taille_ko=12.5,
                    date_modification=datetime(2026, 10, 8, tzinfo=timezone.utc),
                ),
                SimpleNamespace(
                    nom="b.csv",
                    dossier_mois="LOAD_LOCK_202604",
                    chemin="input/LOAD_LOCK_202604/b.csv",
                    taille_ko=3.0,
                    date_modification=datetime(2026, 10, 8, tzinfo=timezone.utc),
                ),
            ]

    fichiers = list_available_files(loader=_LoaderFactice())
    groupes = group_files_by_load_lock(fichiers)

    assert fichiers[0]["source"] == "input/LOAD_LOCK_202603/a.xlsx"
    assert set(groupes) == {"LOAD_LOCK_202603", "LOAD_LOCK_202604"}
    assert groupes["LOAD_LOCK_202603"][0]["filename"] == "a.xlsx"


def test_audit_service_nutilise_aucun_subprocess():
    source = (Path(__file__).parents[1] / "dashboard" / "audit_service.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in source
