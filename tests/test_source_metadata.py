import pandas as pd

from ingestion.local_loader import LocalLoader


def test_excel_loader_sidecar_preserves_real_sheet_column_order_and_row_mapping(tmp_path):
    path = tmp_path / "vehicles.xlsx"
    pd.DataFrame({"Model": ["A", "B"], "Monthly Rent": [100, 200]}).to_excel(
        path,
        sheet_name="Vehicles",
        index=False,
    )
    loader = LocalLoader(tmp_path)

    frame = loader.charger(path)
    metadata = loader.source_metadata(path)

    assert list(frame.columns) == ["Model", "Monthly Rent"]
    assert metadata == {
        "sheet_name": "Vehicles",
        "columns_in_source_order": ["Model", "Monthly Rent"],
        "row_count": 2,
        "header_rows": 1,
        "row_mapping_status": "exact",
    }


def test_csv_loader_sidecar_never_invents_a_sheet(tmp_path):
    path = tmp_path / "vehicles.csv"
    path.write_text("Model,Price\nA,10\n", encoding="utf-8")
    loader = LocalLoader(tmp_path)

    loader.charger(path)
    metadata = loader.source_metadata(path)

    assert metadata["sheet_name"] is None
    assert metadata["columns_in_source_order"] == ["Model", "Price"]
    assert metadata["row_mapping_status"] == "exact"
