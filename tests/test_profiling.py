"""Tests du périmètre d'inventaire sans accès local ou S3 réel."""

from types import SimpleNamespace

import pandas as pd

import ingestion.profiling as profiling


def test_inventaire_utilise_le_loader_et_la_selection_fournis(monkeypatch, tmp_path):
    selection = [
        SimpleNamespace(
            nom="selection.xlsx",
            dossier_mois="LOAD_LOCK_202603",
            taille_ko=12.5,
            chemin="input/LOAD_LOCK_202603/selection.xlsx",
        )
    ]
    chargements = []

    class _LoaderActifFactice:
        def lister_fichiers(self):
            raise AssertionError("La sélection fournie ne doit pas être relistée")

        def charger(self, chemin):
            chargements.append(chemin)
            return pd.DataFrame({"name": ["vehicle"]})

    monkeypatch.setattr(profiling, "RESULTATS_DIR", tmp_path)
    monkeypatch.setattr(profiling, "FICHIER_SORTIE", tmp_path / "inventaire.csv")

    inventaire = profiling.construire_inventaire(
        loader=_LoaderActifFactice(),
        fichiers=selection,
    )

    assert chargements == ["input/LOAD_LOCK_202603/selection.xlsx"]
    assert inventaire["nom_fichier"].tolist() == ["selection.xlsx"]
    assert (tmp_path / "inventaire.csv").is_file()
