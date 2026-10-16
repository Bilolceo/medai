"""app.services.sheet_report: the dashboard's report as one sheet line a day."""

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import sheet_report
from app.services.appointment import CLINIC_TIMEZONE
from app.services.sheets import _sheets_serial
from tests.conftest import Seed


async def test_a_days_line_carries_the_dashboard_figures(
    db_session: AsyncSession, seed: Seed
) -> None:
    day = datetime.now(UTC).astimezone(CLINIC_TIMEZONE).date()
    cells = await sheet_report.report_cells(db_session, seed.tenant_a.id, day)

    assert len(cells) == len(sheet_report.REPORT_HEADER)
    assert cells[0] == _sheets_serial(day)
    assert isinstance(cells[1], str) and cells[1]
    # Counts are numbers the sheet can sum, not text.
    for value in cells[2:11]:
        assert isinstance(value, int | float)


class FakeMirror:
    """Answers the three calls upsert_day makes, and records the writes."""

    def __init__(self, rows: list[list[Any]], tabs: dict[str, int]) -> None:
        self.rows = rows
        self.tabs = tabs
        self.writes: list[tuple[str, str, Any]] = []

    async def worksheet_ids(self, client: httpx.AsyncClient) -> dict[str, int]:
        return dict(self.tabs)

    async def _create_worksheet(self, client: httpx.AsyncClient, title: str, columns: int) -> int:
        self.tabs[title] = 7
        return 7

    async def _call(self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET":
            return {"values": self.rows}
        self.writes.append((method, path, kwargs.get("json")))
        return {}


@pytest.mark.parametrize("already_there", [True, False])
async def test_a_day_is_rewritten_in_place_or_added_once(already_there: bool) -> None:
    day = date(2026, 10, 16)
    rows = [["Sana"], [_sheets_serial(date(2026, 10, 15))]]
    if already_there:
        rows.append([_sheets_serial(day)])
    mirror = FakeMirror(rows, {sheet_report.REPORT_SHEET: 7})
    cells = [_sheets_serial(day), "Payshanba", 5]

    await sheet_report.upsert_day(mirror, cells, day)  # type: ignore[arg-type]

    assert len(mirror.writes) == 1
    method, path, body = mirror.writes[0]
    if already_there:
        assert method == "PUT" and path.startswith(f"/values/{sheet_report.REPORT_SHEET}!A3:")
    else:
        assert method == "POST" and path.endswith(":append")
    assert body == {"values": [cells]}


async def test_the_tab_is_made_with_its_header_when_missing() -> None:
    mirror = FakeMirror([["Sana"]], {})
    await sheet_report.upsert_day(mirror, [1, "x"], date(2026, 10, 16))  # type: ignore[arg-type]

    header_writes = [w for w in mirror.writes if w[0] == "PUT" and w[1].endswith("1")]
    assert header_writes and header_writes[0][2] == {"values": [list(sheet_report.REPORT_HEADER)]}
