"""The "Hisobot" tab: the dashboard's report, one line a day, in the clinic's sheet.

The owner asked for the report where they already look -- the spreadsheet --
as well as on the dashboard. So the figures are not computed a second way
here: each line is the dashboard report (app.api.admin.reports) for that one
day, plus the two counts the owner reads first, how many came and how many
left a number.

Today's line is rewritten every hour as the day fills in, and yesterday's
once more after midnight so its last hours are counted. A mirror, never a
source: the sheet is not read back, and a line someone edits by hand is
simply overwritten on the next run.
"""

import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment, AppointmentStatus
from app.models.lead import Lead
from app.services.appointment import CLINIC_TIMEZONE
from app.services.sheets import (
    TIMEOUT_SECONDS,
    WEEKDAYS_UZ,
    SheetsError,
    SheetsMirror,
    _rgb,
    _sheets_serial,
    get_mirror,
)

logger = logging.getLogger(__name__)

REPORT_SHEET = "Hisobot"
REPORT_HEADER = (
    "Sana",
    "Hafta kuni",
    "Yozgan bemorlar",
    "Yangi bemorlar",
    "Bemor xabarlari",
    "Raqam qoldirdi",
    "Qabulga yozildi",
    "Qabulga o'tish, %",
    "Keldi",
    "Kelmadi",
    "Rasm yuborgan",
    "Javob vaqti, daq",
    "Xodim javoblari, %",
    "Asosiy murojaatlar",
)
_LAST_COLUMN = "N"
_WIDTHS = (100, 100, 120, 110, 110, 110, 120, 120, 70, 80, 110, 110, 120, 380)


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=CLINIC_TIMEZONE)
    return start, start + timedelta(days=1)


async def report_cells(session: AsyncSession, tenant_id: uuid.UUID, day: date) -> list[Any]:
    """One day's line, from the same figures the dashboard shows."""
    # Imported here: the report lives with its API route, and that module
    # pulls in FastAPI; the worker only needs it when a sheet is configured.
    from app.api.admin.reports import _figures
    from app.services.complaints import CATEGORIES, UNKNOWN

    figures = await _figures(session, tenant_id, day, day)
    start, end = _bounds(day)
    leads = (
        await session.execute(
            select(func.count())
            .select_from(Lead)
            .where(Lead.tenant_id == tenant_id, Lead.created_at >= start, Lead.created_at < end)
        )
    ).scalar_one()
    outcome_rows = (
        await session.execute(
            select(Appointment.status, func.count())
            .where(
                Appointment.tenant_id == tenant_id,
                Appointment.scheduled_at >= start,
                Appointment.scheduled_at < end,
            )
            .group_by(Appointment.status)
        )
    ).all()
    outcomes: dict[str, int] = {str(status): int(count) for status, count in outcome_rows}
    labels = {category.key: category.label for category in (*CATEGORIES, UNKNOWN)}
    top = [
        f"{labels.get(key, key)} ({count})"
        for key, count in figures["complaint_counts"].most_common()
        if key != UNKNOWN.key
    ][:3]
    median = figures["median_response"]
    return [
        _sheets_serial(day),
        WEEKDAYS_UZ[day.weekday()],
        len(figures["active"]),
        len(figures["new"]),
        figures["patient_messages"],
        leads,
        len(figures["appointments"]),
        round(figures["conversion"], 1),
        outcomes.get(AppointmentStatus.COMPLETED, 0),
        outcomes.get(AppointmentStatus.NO_SHOW, 0),
        figures["photo_patients"],
        round(median / 60, 1) if median is not None else "",
        round(figures["human_share"], 1),
        ", ".join(top),
    ]


def _layout(gid: int) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _rgb("#0B2545"),
                        "textFormat": {"bold": True, "foregroundColor": _rgb("#FFFFFF")},
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": (
                    "userEnteredFormat"
                    "(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": gid,
                    "startRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
                "cell": {
                    "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"}}
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
    ]
    for index, pixels in enumerate(_WIDTHS):
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": gid,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": pixels},
                    "fields": "pixelSize",
                }
            }
        )
    return requests


async def _ensure(mirror: SheetsMirror, client: httpx.AsyncClient) -> None:
    sheets = await mirror.worksheet_ids(client)
    if REPORT_SHEET not in sheets:
        gid = await mirror._create_worksheet(client, REPORT_SHEET, columns=len(REPORT_HEADER))
        await mirror._call(
            client,
            "PUT",
            f"/values/{REPORT_SHEET}!A1:{_LAST_COLUMN}1",
            params={"valueInputOption": "RAW"},
            json={"values": [list(REPORT_HEADER)]},
        )
        try:
            await mirror._call(client, "POST", ":batchUpdate", json={"requests": _layout(gid)})
        except SheetsError as exc:
            logger.warning("sheets_report_styling_skipped error=%s", exc)


async def _row_for(mirror: SheetsMirror, client: httpx.AsyncClient, day: date) -> int | None:
    body = await mirror._call(
        client,
        "GET",
        f"/values/{REPORT_SHEET}!A:A",
        params={"majorDimension": "ROWS", "valueRenderOption": "UNFORMATTED_VALUE"},
    )
    wanted = _sheets_serial(day)
    for index, values in enumerate(body.get("values") or [], start=1):
        if values and values[0] == wanted:
            return index
    return None


async def upsert_day(mirror: SheetsMirror, cells: list[Any], day: date) -> None:
    """Write one day's line: in place if the day is there, else at the end."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        await _ensure(mirror, client)
        row = await _row_for(mirror, client, day)
        if row is not None:
            await mirror._call(
                client,
                "PUT",
                f"/values/{REPORT_SHEET}!A{row}:{_LAST_COLUMN}{row}",
                params={"valueInputOption": "RAW"},
                json={"values": [cells]},
            )
            return
        await mirror._call(
            client,
            "POST",
            f"/values/{REPORT_SHEET}!A:{_LAST_COLUMN}:append",
            # No INSERT_ROWS: an inserted row takes the header's format and
            # the date shows as 46311. The default fills the blank rows the
            # layout already formatted as dates (see app.services.sheets).
            params={"valueInputOption": "RAW"},
            json={"values": [cells]},
        )


async def mirror_report(session: AsyncSession, tenant_id: uuid.UUID, days: list[date]) -> None:
    """Bring these days' lines up to date. Never raises: the sheet is a copy."""
    mirror = get_mirror()
    if mirror is None:
        return
    for day in days:
        try:
            cells = await report_cells(session, tenant_id, day)
            await upsert_day(mirror, cells, day)
        except Exception:  # noqa: BLE001 - a copy that is briefly behind is fine
            logger.exception("sheets_report_failed day=%s", day)
            return
    logger.info("sheets_report_updated days=%s", [d.isoformat() for d in days])
