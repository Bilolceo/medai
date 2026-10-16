"""app.services.sheet_design: the clinic's sheet drawn like the owner's template."""

from datetime import time

from app.services import sheet_design
from app.services.sheet_report import REPORT_HEADER, report_layout


def test_the_schedule_has_every_slot_of_the_clinic_day() -> None:
    slots = sheet_design.slots()
    assert slots[0] == time(9, 0)
    assert slots[-1] == time(16, 40)
    assert len(slots) == 24


def test_each_slot_reads_the_book_for_the_chosen_day() -> None:
    formula = sheet_design.slot_formula(7, "A")
    assert formula.startswith("=IFERROR(TEXTJOIN(")
    assert "'Qabullar'!$A$2:$A" in formula
    assert "'Qabullar'!$C$2:$C=$H$2" in formula
    # 20-minute slots: 72 to a day, an odd minute falls into its slot.
    assert "INT('Qabullar'!$D$2:$D*72+1/10000)" in formula
    assert "ROUND($B7*72)" in formula


def test_no_formula_carries_a_decimal_point() -> None:
    """A ru_RU sheet reads "0.5" as a parse error: the decimal mark is a comma."""
    import re

    formulas = [c for row in sheet_design.schedule_values(";") for c in row if isinstance(c, str)]
    formulas += [
        r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0][
            "userEnteredValue"
        ]
        for r in sheet_design.schedule_requests(1, ";")
        if "addConditionalFormatRule" in r
        and r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["type"]
        == "CUSTOM_FORMULA"
    ]
    assert not [f for f in formulas if f.startswith("=") and re.search(r"\d\.\d", f)]


def test_formulas_are_written_in_the_sheets_own_dialect() -> None:
    grid = sheet_design.schedule_values(";")
    cell = grid[6][2]
    assert ";" in cell and ", " in cell  # the separator, and the joining text kept
    assert grid[6][1] == 9 / 24
    assert grid[1][7] == "=TODAY()"
    assert grid[3][1:8] == [name for name, _ in sheet_design.CARDS]
    assert grid[5][1:8] == list(sheet_design.HEADER)


def test_the_cards_count_the_chosen_days_bookings() -> None:
    total, came, waiting, missed, cancelled, free, rate = sheet_design.card_formulas()
    assert '"<>Bekor qilindi"' in total
    assert '"Keldi"' in came and '"Kutilmoqda"' in waiting
    assert '"Kelmadi"' in missed and '"Bekor qilindi"' in cancelled
    assert "COUNTBLANK(D7:D30)" in free
    assert rate == "=IFERROR(C5/(C5+E5),0)"


def test_the_title_line_names_the_day_in_uzbek() -> None:
    line = sheet_design.title_line()
    assert '"Dushanba"' in line and '"Yakshanba"' in line
    assert "09:00 – 17:00" in line and "20 daqiqa" in line


def test_the_report_lines_are_formatted_apart_from_the_header() -> None:
    """Live, 2026-10-16: every Hisobot line came out in the header's navy."""
    requests = report_layout(5)
    painted = [r["repeatCell"] for r in requests if "repeatCell" in r]
    header = next(p for p in painted if p["range"].get("endRowIndex") == 1)
    body = next(
        p
        for p in painted
        if p["range"].get("startRowIndex") == 1
        and p["range"].get("endColumnIndex") == len(REPORT_HEADER)
    )
    assert header["cell"]["userEnteredFormat"]["backgroundColor"] != (
        body["cell"]["userEnteredFormat"]["backgroundColor"]
    )
    assert body["cell"]["userEnteredFormat"]["textFormat"]["bold"] is False
    assert any("addBanding" in r for r in requests)
    assert any("setBasicFilter" in r for r in requests)


def test_month_markers_take_the_lines_colours() -> None:
    [request] = sheet_design.marker_requests(3, [12])
    cell = request["repeatCell"]
    assert cell["range"]["startRowIndex"] == 12 and cell["range"]["startColumnIndex"] == 7
    assert cell["cell"]["userEnteredFormat"]["textFormat"]["bold"] is True
