"""The appointment book, kept month by month.

Qabullar reads like a diary: a "Sentabr 2026" line, that month's bookings
in the order the day goes, then "Oktabr 2026" and its bookings under it.
The Home page's month tiles jump to those lines.

So a new booking is not appended at the bottom any more -- a booking taken
in September for the 3rd of October belongs under October, which may not
have a line yet. This module works out where a booking goes and builds the
requests that put it there. It never talks to Google itself
(app.services.sheets does), so every decision here can be tested on plain
lists.

A month line is recognised by a marker in the hidden code column, "OY",
as well as by its words: the marker is what the bot writes, and it cannot
be mistaken for a booking's code; the words are what a person typing a
line by hand would write.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

MONTHS_UZ = (
    "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
    "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr",
)  # fmt: skip

# In the hidden code column (H) of a month line.
MARKER = "OY"
# The columns a month line's words span: everything but the hidden code.
_LINE_COLUMNS = 7
_KEY_COLUMN = 7
_DATE_COLUMN = 2
_TIME_COLUMN = 3

_EPOCH = date(1899, 12, 30)
_LABEL = re.compile(r"^\s*(" + "|".join(MONTHS_UZ) + r")\s+(\d{4})\s*$", re.IGNORECASE)

# The look of a month line: the teal of the book's header, softened, so it
# reads as a divider rather than as another header row.
LINE_FILL = {"red": 0.8, "green": 0.984, "blue": 0.945}  # #CCFBF1
LINE_INK = {"red": 0.059, "green": 0.463, "blue": 0.431}  # #0F766E


def label(year: int, month: int) -> str:
    return f"{MONTHS_UZ[month - 1]} {year}"


def serial(day: date) -> int:
    return (day - _EPOCH).days


def _as_day(value: Any) -> float | None:
    """A date cell as Sheets' day number, whether it holds a date or text
    that says one ("2026-09-26", "26.09.2026", "Shanba 26.09.2026")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value or "").strip()
    for pattern, fmt in (
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{2}\.\d{2}\.\d{4}", "%d.%m.%Y"),
    ):
        found = re.search(pattern, text)
        if found:
            try:
                return float(serial(datetime.strptime(found.group(0), fmt).date()))
            except ValueError:
                return None
    return None


def _as_time(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    found = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    return (int(found.group(1)) * 60 + int(found.group(2))) / 1440 if found else 0.0


def month_of(day_serial: float) -> tuple[int, int]:
    day = _EPOCH + timedelta(days=int(day_serial))
    return day.year, day.month


@dataclass(frozen=True)
class BookRow:
    """One row of Qabullar as far as order goes. `number` is 1-based."""

    number: int
    month: tuple[int, int] | None = None  # set on a month line
    day: float | None = None  # set on a booking with a date
    time: float = 0.0
    empty: bool = False

    @property
    def is_line(self) -> bool:
        return self.month is not None


def parse(values: Sequence[Sequence[Any]]) -> list[BookRow]:
    """Rows 2 onwards of Qabullar, read unformatted, as BookRows."""
    rows: list[BookRow] = []
    for number, raw in enumerate(values[1:], start=2):
        cells = list(raw) + [""] * (8 - len(raw))
        if not any(str(cell).strip() for cell in cells):
            rows.append(BookRow(number, empty=True))
            continue
        found = _LABEL.match(str(cells[0]))
        is_line = str(cells[_KEY_COLUMN]).strip() == MARKER or (
            found is not None and not str(cells[_DATE_COLUMN]).strip()
        )
        if is_line and found is not None:
            month = MONTHS_UZ.index(found.group(1).capitalize()) + 1
            rows.append(BookRow(number, month=(int(found.group(2)), month)))
            continue
        rows.append(
            BookRow(number, day=_as_day(cells[_DATE_COLUMN]), time=_as_time(cells[_TIME_COLUMN]))
        )
    return rows


@dataclass(frozen=True)
class Placement:
    """Where a booking goes: the row it will occupy, and whether a month
    line has to go in first -- at `line_at`, pushing the booking one down."""

    row: int
    line_at: int | None = None


def place(rows: Sequence[BookRow], day: float, time: float) -> Placement:
    """Where a booking on `day` at `time` belongs in the book."""
    target = month_of(day)
    lines = [row for row in rows if row.is_line]
    last_filled = max((row.number for row in rows if not row.empty), default=1)
    own = next((row for row in lines if row.month == target), None)
    if own is None:
        later = next((row for row in lines if row.month and row.month > target), None)
        at = later.number if later is not None else last_filled + 1
        return Placement(row=at + 1, line_at=at)
    following = next((row.number for row in lines if row.number > own.number), None)
    block = [
        row
        for row in rows
        if own.number < row.number < (following or 10**9) and not row.empty and not row.is_line
    ]
    for row in block:
        if (row.day or 0, row.time) > (day, time):
            return Placement(row=row.number)
    return Placement(row=(block[-1].number if block else own.number) + 1)


def block_month(rows: Sequence[BookRow], number: int) -> tuple[int, int] | None:
    """The month line a row sits under, if any."""
    current: tuple[int, int] | None = None
    for row in rows:
        if row.number > number:
            break
        if row.is_line:
            current = row.month
    return current


# --- requests --------------------------------------------------------------------------


def _range(gid: int, row: int, first: int = 0, last: int = 8) -> dict[str, int]:
    """0-based row; columns [first, last)."""
    return {
        "sheetId": gid,
        "startRowIndex": row,
        "endRowIndex": row + 1,
        "startColumnIndex": first,
        "endColumnIndex": last,
    }


def _insert(gid: int, row: int, inherit_from_before: bool) -> dict[str, Any]:
    return {
        "insertDimension": {
            "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": row, "endIndex": row + 1},
            "inheritFromBefore": inherit_from_before,
        }
    }


def line_requests(gid: int, number: int, year: int, month: int) -> list[dict[str, Any]]:
    """A month line written into an (already inserted) row `number`."""
    row = number - 1
    cells: list[dict[str, Any]] = [
        {
            "userEnteredValue": {"stringValue": label(year, month)},
            "userEnteredFormat": {
                "backgroundColor": LINE_FILL,
                "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": LINE_INK},
                "verticalAlignment": "MIDDLE",
                "horizontalAlignment": "LEFT",
            },
        }
    ]
    cells += [{"userEnteredFormat": {"backgroundColor": LINE_FILL}}] * (_LINE_COLUMNS - 1)
    cells.append(
        {
            "userEnteredValue": {"stringValue": MARKER},
            "userEnteredFormat": {
                "backgroundColor": LINE_FILL,
                "textFormat": {"bold": True, "foregroundColor": LINE_INK},
                "verticalAlignment": "MIDDLE",
            },
        }
    )
    return [
        {
            "updateCells": {
                "range": _range(gid, row),
                "rows": [{"values": cells}],
                "fields": "userEnteredValue,userEnteredFormat",
            }
        },
        # No status dropdown on a divider.
        {"setDataValidation": {"range": _range(gid, row)}},
        {"mergeCells": {"range": _range(gid, row, 0, _LINE_COLUMNS), "mergeType": "MERGE_ALL"}},
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": gid,
                    "dimension": "ROWS",
                    "startIndex": row,
                    "endIndex": row + 1,
                },
                "properties": {"pixelSize": 34},
                "fields": "pixelSize",
            }
        },
    ]


def booking_cells(
    name: str,
    phone: str,
    day: float,
    time: float,
    channel: str,
    status: str,
    comment: str,
    reference: str,
    date_pattern: str,
) -> list[dict[str, Any]]:
    """A booking's eight cells as typed values: a real date and time, the
    phone as text so its "+" survives."""

    def text(value: str) -> dict[str, Any]:
        return {"userEnteredValue": {"stringValue": value}}

    return [
        text(name),
        text(phone),
        {
            "userEnteredValue": {"numberValue": day},
            "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": date_pattern}},
        },
        {
            "userEnteredValue": {"numberValue": time},
            "userEnteredFormat": {"numberFormat": {"type": "TIME", "pattern": "HH:mm"}},
        },
        text(channel),
        text(status),
        text(comment),
        text(reference),
    ]


def insert_requests(
    gid: int,
    rows: Sequence[BookRow],
    placement: Placement,
    cells: list[dict[str, Any]],
    template_row: int,
    new_month: tuple[int, int] | None,
) -> list[dict[str, Any]]:
    """One atomic batch: the month line if it is missing, then the booking.

    Atomic because two bookings taken in the same second each insert their
    own row before writing into it; neither can write into the other's.
    The booking's look -- number formats, the status dropdown -- is copied
    from `template_row` (0-based), a formatted row below everything, so a
    booking under a month line never inherits the line's colour.
    """
    requests: list[dict[str, Any]] = []
    if placement.line_at is not None and new_month is not None:
        requests.append(_insert(gid, placement.line_at - 1, inherit_from_before=False))
        requests += line_requests(gid, placement.line_at, *new_month)
    row = placement.row - 1
    requests.append(_insert(gid, row, inherit_from_before=False))
    template = {
        "sheetId": gid,
        "startRowIndex": template_row + 1,  # pushed down by the insert(s) above
        "endRowIndex": template_row + 2,
        "startColumnIndex": 0,
        "endColumnIndex": 8,
    }
    if placement.line_at is not None:
        template = {
            **template,
            "startRowIndex": template_row + 2,
            "endRowIndex": template_row + 3,
        }
    for paste in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
        requests.append(
            {
                "copyPaste": {
                    "source": template,
                    "destination": _range(gid, row),
                    "pasteType": paste,
                }
            }
        )
    requests.append(
        {
            "updateCells": {
                "range": _range(gid, row),
                "rows": [{"values": cells}],
                "fields": "userEnteredValue,userEnteredFormat.numberFormat",
            }
        }
    )
    return requests


def line_rule(gid: int) -> dict[str, Any]:
    """Month lines coloured by rule as well: the book's alternating colours
    would otherwise paint over them. A formula with no separator in it, so
    the spreadsheet's locale cannot break it."""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [
                    {
                        "sheetId": gid,
                        "startRowIndex": 1,
                        "endRowIndex": 5000,
                        "startColumnIndex": 0,
                        "endColumnIndex": 8,
                    }
                ],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=$H2="{MARKER}"'}],
                    },
                    "format": {
                        "backgroundColor": LINE_FILL,
                        "textFormat": {"bold": True, "foregroundColor": LINE_INK},
                    },
                },
            },
            "index": 0,
        }
    }


# --- putting an existing book in order ----------------------------------------------------


@dataclass(frozen=True)
class Tidy:
    """What organising the book takes, in the order it has to happen."""

    delete: list[int]  # 1-based rows to remove, bottom first: gaps and old lines
    text_dates: dict[int, float]  # 1-based row (before deletions) -> the date it says
    months: list[tuple[int, int]]  # the months present, in order


def plan(rows: Sequence[BookRow], raw: Sequence[Sequence[Any]]) -> Tidy:
    """Everything to remove, and every date to turn from text into a date."""
    last_filled = max((row.number for row in rows if not row.empty), default=1)
    delete = sorted(
        (row.number for row in rows if row.number <= last_filled and (row.empty or row.is_line)),
        reverse=True,
    )
    text_dates: dict[int, float] = {}
    for row in rows:
        if row.day is None or row.is_line:
            continue
        cells = list(raw[row.number - 1]) + [""] * 8
        if not isinstance(cells[_DATE_COLUMN], int | float):
            text_dates[row.number] = row.day
    months = sorted({month_of(row.day) for row in rows if row.day is not None and not row.is_line})
    return Tidy(delete=delete, text_dates=text_dates, months=months)
