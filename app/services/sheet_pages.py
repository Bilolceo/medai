"""The front page of the clinic's spreadsheet: the year and its months.

The appointment book (Qabullar) is kept month by month -- a "Sentabr 2026"
line with that month's bookings under it, then October's (see
app.services.sheet_months). Home is the way in: the year, and a tile per
month that jumps to that month's line in Qabullar.

Nothing on Home has to be rebuilt as the months go by. Each tile finds its
month's line with MATCH, so it still lands on the right row after bookings
are inserted above it; it counts its month's bookings with COUNTIFS; and it
lights up by colour rule the moment the bot opens the month's line, and
turns solid on the month the calendar is in. Built once per year:
`python -m app.services.sheet_pages 2026`.
"""

import asyncio
import logging
import sys
from collections.abc import Sequence
from typing import Any

import httpx

from app.services.sheet_months import MONTHS_UZ, label
from app.services.sheets import APPOINTMENT_SHEET, SheetsMirror, _rgb

logger = logging.getLogger(__name__)

HOME_SHEET = "Home"

# The palette of the rest of the spreadsheet: the teal of the Lidlar and
# Qabullar headers, a slate ink, and quiet greys for what is not there yet.
TEAL = "#0F766E"
TEAL_SOFT = "#CCFBF1"
INK = "#0F172A"
MUTED = "#64748B"
FAINT = "#94A3B8"
LINE = "#E2E8F0"
NOT_YET = "#F1F5F9"
WHITE = "#FFFFFF"

#   A      B     C   D     E   F       G   H       I
#   margin tile  gap tile  gap tile    gap tile    margin
_WIDTHS = (32, 190, 18, 190, 18, 190, 18, 190, 32)
_TILE_COLUMNS = (1, 3, 5, 7)
_YEAR_ROW, _FIRST_TILE_ROW = 4, 6

# --- formulas ----------------------------------------------------------------------


def localise(formula: str, separator: str) -> str:
    """Written with commas; stored with the spreadsheet's own separator.

    The API parses a formula the way the spreadsheet's locale would: on the
    clinic's ru_RU spreadsheet every argument separator is ";" and a formula
    with commas is #ERROR!. Commas inside a quoted string are text, not
    separators, and stay.
    """
    if separator == ",":
        return formula
    out: list[str] = []
    quoted = False
    for character in formula:
        if character == '"':
            quoted = not quoted
        out.append(separator if character == "," and not quoted else character)
    return "".join(out)


def separator_for(locale: str | None) -> str:
    """ "," where the decimal point is a dot, ";" where it is a comma."""
    dot_decimal = ("en", "ja", "zh", "ko", "th", "he", "iw", "hi", "ms", "fil", "ga")
    return "," if (locale or "en").split("_")[0].lower() in dot_decimal else ";"


def _where(year: int, month: int) -> str:
    """The row of the month's line in Qabullar, or an error if it has none."""
    return f'MATCH("{label(year, month)}", {APPOINTMENT_SHEET}!A:A, 0)'


def _link(book_gid: int, year: int, month: int, text: str) -> str:
    return f'HYPERLINK("#gid={book_gid}&range=A" & {_where(year, month)}, {text})'


def tile_name(book_gid: int, year: int, month: int) -> str:
    """The month's name: a link to its line once it has one, plain before."""
    name = f'"{MONTHS_UZ[month - 1]}"'
    return f"=IFERROR({_link(book_gid, year, month, name)}, {name})"


def tile_count(book_gid: int, year: int, month: int) -> str:
    """ "12 ta qabul →" under a month with a line; "Tez orada" before."""
    days = f"{APPOINTMENT_SHEET}!C:C"
    count = (
        f'COUNTIFS({days}, ">=" & DATE({year}, {month}, 1), '
        f'{days}, "<" & DATE({year}, {month + 1}, 1))'
    )
    text = f'{count} & " ta qabul  →"'
    return f'=IFERROR({_link(book_gid, year, month, text)}, "Tez orada")'


def _has_line(year: int, month: int) -> str:
    """For a colour rule, which can reach another sheet only through INDIRECT."""
    return f'ISNUMBER(MATCH("{label(year, month)}", INDIRECT("{APPOINTMENT_SHEET}!A:A"), 0))'


def _is_now(year: int, month: int) -> str:
    return f"AND(YEAR(TODAY()) = {year}, MONTH(TODAY()) = {month})"


# --- what goes where -------------------------------------------------------------------


def tile_position(month: int) -> tuple[int, int]:
    """(row of the month's name, column) for a month's tile, 0-based."""
    index = month - 1
    return _FIRST_TILE_ROW + (index // 4) * 3, _TILE_COLUMNS[index % 4]


def home_values(year: int, book_gid: int, brand: str = "") -> dict[tuple[int, int], str]:
    """Cell (row, column) -> what it holds. `brand` is the doctor's name for
    the title (see app.services.branding)."""
    cells: dict[tuple[int, int], str] = {
        (1, 1): f"{brand} — qabullar" if brand else "Qabullar",
        (2, 1): "Oyni tanlang: o‘sha oyning qabullari Qabullar varag‘ida ochiladi.",
        (_YEAR_ROW, 1): str(year),
    }
    for month in range(1, 13):
        row, column = tile_position(month)
        cells[(row, column)] = tile_name(book_gid, year, month)
        cells[(row + 1, column)] = tile_count(book_gid, year, month)
    footer = tile_position(12)[0] + 3
    cells[(footer, 1)] = (
        "Yangi qabullar bot orqali o‘z oyiga tushadi; yangi oy boshlansa, u shu yerda o‘zi yonadi."
    )
    return cells


# --- the look -------------------------------------------------------------------------


def _cell_range(gid: int, row: int, col: int, rows: int = 1, cols: int = 1) -> dict[str, int]:
    return {
        "sheetId": gid,
        "startRowIndex": row,
        "endRowIndex": row + rows,
        "startColumnIndex": col,
        "endColumnIndex": col + cols,
    }


def _format(range_: dict[str, int], fmt: dict[str, Any], fields: str) -> dict[str, Any]:
    return {"repeatCell": {"range": range_, "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _text(size: int = 10, bold: bool = False, colour: str = INK) -> dict[str, Any]:
    return {
        "fontFamily": "Inter",
        "fontSize": size,
        "bold": bold,
        "underline": False,
        "foregroundColorStyle": {"rgbColor": _rgb(colour)},
    }


def _height(gid: int, start: int, end: int, pixels: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def _width(gid: int, column: int, pixels: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": gid,
                "dimension": "COLUMNS",
                "startIndex": column,
                "endIndex": column + 1,
            },
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def _rule(
    range_: dict[str, int], formula: str, fill: str, ink: str, bold: bool, separator: str
) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [range_],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": localise("=" + formula, separator)}],
                    },
                    "format": {
                        "backgroundColorStyle": {"rgbColor": _rgb(fill)},
                        "textFormat": {
                            "foregroundColorStyle": {"rgbColor": _rgb(ink)},
                            "bold": bold,
                        },
                    },
                },
            },
            "index": 0,
        }
    }


_TEXT_FIELDS = "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment)"
_TILE_FIELDS = (
    "userEnteredFormat(backgroundColorStyle,textFormat,horizontalAlignment,"
    "verticalAlignment,padding)"
)


def _tile(gid: int, year: int, month: int, separator: str) -> list[dict[str, Any]]:
    """One tile: grey until its month has a line in Qabullar; then white
    with teal; solid teal while the calendar is in that month."""
    row, column = tile_position(month)
    name, count = _cell_range(gid, row, column), _cell_range(gid, row + 1, column)
    grey = {"backgroundColorStyle": {"rgbColor": _rgb(NOT_YET)}}
    exists = _has_line(year, month)
    now = f"AND({exists}, {_is_now(year, month)})"
    return [
        _format(
            name,
            {
                **grey,
                "textFormat": _text(15, True, FAINT),
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "BOTTOM",
                "padding": {"left": 14, "top": 6},
            },
            _TILE_FIELDS,
        ),
        _format(
            count,
            {
                **grey,
                "textFormat": _text(10, False, FAINT),
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "TOP",
                "padding": {"left": 14, "bottom": 6},
            },
            _TILE_FIELDS,
        ),
        {
            "updateBorders": {
                "range": _cell_range(gid, row, column, rows=2),
                **{
                    side: {"style": "SOLID", "colorStyle": {"rgbColor": _rgb(LINE)}}
                    for side in ("top", "bottom", "left", "right")
                },
            }
        },
        # Each added at index 0, so the one added last is checked first:
        # "this month" wins over "has a line".
        _rule(name, exists, WHITE, TEAL, True, separator),
        _rule(count, exists, WHITE, MUTED, False, separator),
        _rule(name, now, TEAL, WHITE, True, separator),
        _rule(count, now, TEAL, TEAL_SOFT, False, separator),
    ]


def home_requests(gid: int, year: int, separator: str) -> list[dict[str, Any]]:
    """The look of Home: title, year, twelve tiles, a footnote."""
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": gid,
                    "gridProperties": {"hideGridlines": True, "frozenRowCount": 0},
                    "tabColorStyle": {"rgbColor": _rgb(TEAL)},
                },
                "fields": (
                    "gridProperties.hideGridlines,gridProperties.frozenRowCount,tabColorStyle"
                ),
            }
        },
        {"mergeCells": {"range": _cell_range(gid, 1, 1, cols=7), "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": _cell_range(gid, 2, 1, cols=7), "mergeType": "MERGE_ALL"}},
        {
            "mergeCells": {
                "range": _cell_range(gid, _YEAR_ROW, 1, cols=7),
                "mergeType": "MERGE_ALL",
            }
        },
        _format(_cell_range(gid, 1, 1), {"textFormat": _text(22, True)}, _TEXT_FIELDS),
        _format(_cell_range(gid, 2, 1), {"textFormat": _text(10, colour=MUTED)}, _TEXT_FIELDS),
        _format(
            _cell_range(gid, _YEAR_ROW, 1, cols=7),
            {
                "textFormat": _text(26, True, TEAL),
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "BOTTOM",
            },
            _TEXT_FIELDS,
        ),
        {
            "updateBorders": {
                "range": _cell_range(gid, _YEAR_ROW, 1, cols=7),
                "bottom": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": _rgb(TEAL)}},
            }
        },
        _height(gid, 0, 1, 24),
        _height(gid, 1, 2, 48),
        _height(gid, 3, 4, 12),
        _height(gid, _YEAR_ROW, _YEAR_ROW + 1, 52),
        _height(gid, _YEAR_ROW + 1, _YEAR_ROW + 2, 18),
    ]
    requests.extend(_width(gid, index, width) for index, width in enumerate(_WIDTHS))
    for month in range(1, 13):
        requests += _tile(gid, year, month, separator)
    for block in range(3):
        top = _FIRST_TILE_ROW + block * 3
        requests += [
            _height(gid, top, top + 1, 46),
            _height(gid, top + 1, top + 2, 30),
            _height(gid, top + 2, top + 3, 18),
        ]
    footer = tile_position(12)[0] + 3
    requests.append(
        _format(_cell_range(gid, footer, 1), {"textFormat": _text(9, colour=FAINT)}, _TEXT_FIELDS)
    )
    return requests


# --- publishing ----------------------------------------------------------------------


def _grid(cells: dict[tuple[int, int], str], separator: str) -> list[list[str]]:
    rows = max(row for row, _ in cells) + 1
    columns = max(column for _, column in cells) + 1
    grid = [["" for _ in range(columns)] for _ in range(rows)]
    for (row, column), value in cells.items():
        grid[row][column] = localise(value, separator) if value.startswith("=") else value
    return grid


class SheetPages:
    """Builds Home in the clinic's spreadsheet."""

    def __init__(self, mirror: SheetsMirror | None = None, *, brand: str = "") -> None:
        self._mirror = mirror or SheetsMirror()
        self._brand = brand

    async def _batch(
        self, client: httpx.AsyncClient, requests: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._mirror._call(client, "POST", ":batchUpdate", json={"requests": requests})

    async def publish(self, year: int) -> int:
        """Rebuild Home for `year`. Returns Home's id.

        Home keeps its id and is cleared in place -- values, formats,
        merges, colour rules -- so a second run draws it once, not twice.
        Month tabs from an earlier design are removed: the months live in
        Qabullar now.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            body = await self._mirror._call(
                client,
                "GET",
                "",
                params={
                    "fields": "properties.locale,sheets(properties(sheetId,title,gridProperties),"
                    "merges,conditionalFormats)"
                },
            )
            separator = separator_for(body.get("properties", {}).get("locale"))
            sheets = {s["properties"]["title"]: s for s in body.get("sheets", [])}
            if APPOINTMENT_SHEET not in sheets:
                raise ValueError(f"no {APPOINTMENT_SHEET} sheet to point the months at")
            book_gid = int(sheets[APPOINTMENT_SHEET]["properties"]["sheetId"])

            old_tabs = [
                sheets[title]["properties"]["sheetId"]
                for title in (label(year, m) for m in range(1, 13))
                if title in sheets
            ]
            if old_tabs:
                await self._batch(client, [{"deleteSheet": {"sheetId": gid}} for gid in old_tabs])

            if HOME_SHEET in sheets:
                home = sheets[HOME_SHEET]
                gid = int(home["properties"]["sheetId"])
            else:
                reply = await self._batch(
                    client, [{"addSheet": {"properties": {"title": HOME_SHEET, "index": 0}}}]
                )
                home = {}
                gid = int(reply["replies"][0]["addSheet"]["properties"]["sheetId"])

            reset: list[dict[str, Any]] = [
                {"deleteConditionalFormatRule": {"sheetId": gid, "index": 0}}
                for _ in home.get("conditionalFormats") or []
            ]
            reset += [{"unmergeCells": {"range": merge}} for merge in home.get("merges") or []]
            reset.append(
                {
                    "updateCells": {
                        "range": {"sheetId": gid},
                        "fields": "userEnteredValue,userEnteredFormat",
                    }
                }
            )
            # Row heights below the tiles back to ordinary: an earlier design
            # drew month sections there.
            grid = home.get("properties", {}).get("gridProperties", {})
            reset.append(_height(gid, 17, int(grid.get("rowCount", 1000)), 21))
            await self._batch(client, reset)

            await self._mirror._call(
                client,
                "PUT",
                f"/values/'{HOME_SHEET}'!A1",
                params={"valueInputOption": "USER_ENTERED"},
                json={"values": _grid(home_values(year, book_gid, self._brand), separator)},
            )
            await self._batch(client, home_requests(gid, year, separator))
            logger.info("sheet_home_built year=%d removed_tabs=%d", year, len(old_tabs))
            return gid


async def _main(argv: Sequence[str]) -> None:
    from app.core.db import db_session
    from app.services import branding

    async with db_session() as session:
        brand = branding.title(await branding.the_tenant(session))
    await SheetPages(brand=brand).publish(int(argv[0]))


if __name__ == "__main__":  # pragma: no cover - a command, run by hand
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 2:
        print("usage: python -m app.services.sheet_pages YEAR")
        sys.exit(2)
    asyncio.run(_main(sys.argv[1:]))
    print("done")
