"""The clinic's spreadsheet drawn the way the owner's template draws it.

The owner sent the sheet they had before (2026-10-16) and asked for this one
to look the same: Home, Lidlar, Qabullar, the day's "Qabul jadvali", in that
order, in its colours. Home, Lidlar and Qabullar are kept by
app.services.sheet_pages and app.services.sheets; this module adds what was
missing and puts right what had drifted:

* "Qabul jadvali" -- one day on one screen: the date at the top, the day's
  figures under it, then every 20-minute slot from 09:00 to 16:40 with
  whoever is booked into it. Nothing is typed into it. Every cell reads
  Qabullar, so it is never out of date and never needs the bot to write it;
  changing the date at the top shows another day.
* "Hisobot" -- the daily report -- in the same teal as the other tabs. Its
  first lines were written while an append still took the header's format,
  and the whole tab came out navy.
* the "OY" marker at the end of a month line, which was left uncoloured.

Run by hand, as often as wanted -- it clears what it drew before:
`python -m app.services.sheet_design`.
"""

import asyncio
import logging
from datetime import time
from typing import Any

import httpx

from app.services.appointment import SLOT_MINUTES, WORK_END, WORK_START
from app.services.sheet_months import LINE_FILL, LINE_INK, MARKER
from app.services.sheet_pages import HOME_SHEET, localise, separator_for
from app.services.sheet_report import REPORT_SHEET, report_layout
from app.services.sheets import (
    _STATUS_COLOURS,
    APPOINTMENT_SHEET,
    VIEW_SHEET,
    SheetsMirror,
    _rgb,
)

logger = logging.getLogger(__name__)

SCHEDULE_SHEET = "Qabul jadvali"
TAB_ORDER = (HOME_SHEET, VIEW_SHEET, APPOINTMENT_SHEET, SCHEDULE_SHEET, REPORT_SHEET)

# The template's own palette for this tab: a calm blue, not the book's teal.
FONT = "Google Sans"
BLUE = "#2C628A"
TITLE = "#2B628A"
MUTED = "#71787D"
CARD = "#F6F8FF"
CARD_LABEL = "#4E6174"
CARD_EDGE = "#CCD8E5"
CARD_INNER = "#E0E8F2"
ROW_ALT = "#F6F8FB"
ROW_LINE = "#E5EDF4"
INK = "#171C1F"
NOW_FILL = "#E8F0FE"

# Columns B..H; A is a margin.
WIDTHS = (30, 95, 190, 155, 220, 135, 135, 145)
HEADER = ("Vaqt", "Bemor F.I.SH", "Telefon", "Murojaat sababi", "Kanal", "Kod", "Holat")
# Which Qabullar column each schedule column shows (after Vaqt).
SOURCE = ("A", "B", "G", "E", "H", "F")

DATE_CELL = "H2"
FIRST_SLOT_ROW = 7  # 1-based
CARDS = (
    ("Jami bemorlar", BLUE),
    ("Keldi", "#007F3F"),
    ("Kutilmoqda", "#CC7F00"),
    ("Kelmadi", "#BF2626"),
    ("Bekor qilindi", "#4B5563"),
    ("Bo'sh vaqtlar", BLUE),
    ("Kelish foizi", BLUE),
)

WEEKDAYS = ("Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba")


def slots() -> list[time]:
    """09:00, 09:20, ... 16:40: the clinic's day."""
    out: list[time] = []
    minutes = WORK_START.hour * 60 + WORK_START.minute
    end = WORK_END.hour * 60 + WORK_END.minute
    while minutes < end:
        out.append(time(minutes // 60, minutes % 60))
        minutes += SLOT_MINUTES
    return out


def _fraction(at: time) -> float:
    return (at.hour * 60 + at.minute) / (24 * 60)


def _per_day() -> int:
    return 24 * 60 // SLOT_MINUTES


def _in_slot(row: int) -> str:
    """Qabullar rows booked on the chosen day, into this row's slot.

    A booking at an odd minute (an operator typing 15:10) falls into the slot
    it starts in, so nothing on the day goes missing from the screen.
    """
    book = f"'{APPOINTMENT_SHEET}'"
    return (
        f"{book}!$C$2:$C=${DATE_CELL[0]}${DATE_CELL[1:]},"
        # 1/10000, not 0.0001: on a ru_RU sheet the decimal mark is a comma
        # and "0.0001" is a parse error (#ERROR! in every slot, 2026-10-16).
        f"IFERROR(INT({book}!$D$2:$D*{_per_day()}+1/10000),-1)=ROUND($B{row}*{_per_day()})"
    )


def slot_formula(row: int, source: str) -> str:
    book = f"'{APPOINTMENT_SHEET}'"
    return f'=IFERROR(TEXTJOIN(", ",TRUE,FILTER({book}!${source}$2:${source},{_in_slot(row)})),"")'


def _count(status: str | None) -> str:
    book = f"'{APPOINTMENT_SHEET}'"
    day = f"{book}!$C:$C,${DATE_CELL[0]}${DATE_CELL[1:]}"
    if status is None:
        return f'COUNTIFS({day},{book}!$F:$F,"<>Bekor qilindi")'
    return f'COUNTIFS({day},{book}!$F:$F,"{status}")'


def card_formulas() -> list[str]:
    last = FIRST_SLOT_ROW + len(slots()) - 1
    day = f"${DATE_CELL[0]}${DATE_CELL[1:]}"
    return [
        "=" + _count(None),
        "=" + _count("Keldi"),
        "=" + _count("Kutilmoqda"),
        "=" + _count("Kelmadi"),
        "=" + _count("Bekor qilindi"),
        # Sunday is not a working day: no free time to offer.
        # Counted on the phone: a booking always has one, not always a name.
        f"=IF(WEEKDAY({day},2)=7,0,COUNTBLANK(D{FIRST_SLOT_ROW}:D{last})"
        f'+COUNTIF(H{FIRST_SLOT_ROW}:H{last},"Bekor qilindi"))',
        "=IFERROR(C5/(C5+E5),0)",
    ]


def title_line() -> str:
    day = f"${DATE_CELL[0]}${DATE_CELL[1:]}"
    names = ",".join(f'"{name}"' for name in WEEKDAYS)
    hours = f"{WORK_START.strftime('%H:%M')} – {WORK_END.strftime('%H:%M')}"
    return (
        f'="📅 "&CHOOSE(WEEKDAY({day},2),{names})&", "&TEXT({day},"dd.mm.yyyy")'
        f'&"   |   ⏰ Qabul vaqti: {hours}   |   ⏱ Har bir qabul: {SLOT_MINUTES} daqiqa"'
        f'&IF(WEEKDAY({day},2)=7,"   |   Yakshanba — dam olish kuni","")'
    )


def schedule_values(separator: str) -> list[list[Any]]:
    """The tab's values from A1, formulas in the spreadsheet's own dialect."""
    blank = [""] * len(WIDTHS)
    grid: list[list[Any]] = [list(blank) for _ in range(FIRST_SLOT_ROW - 1 + len(slots()))]
    grid[1][1] = "Dr. Temur — Bemorlar Qabuli Jadvali"
    grid[1][6] = "Kun:"
    grid[1][7] = "=TODAY()"
    grid[2][1] = localise(title_line(), separator)
    for index, (name, _) in enumerate(CARDS):
        grid[3][1 + index] = name
    for index, formula in enumerate(card_formulas()):
        grid[4][1 + index] = localise(formula, separator)
    for index, name in enumerate(HEADER):
        grid[5][1 + index] = name
    for offset, at in enumerate(slots()):
        row = FIRST_SLOT_ROW + offset
        line = grid[row - 1]
        line[1] = _fraction(at)
        for index, source in enumerate(SOURCE):
            line[2 + index] = localise(slot_formula(row, source), separator)
    return grid


def _range(gid: int, row: int, col: int, rows: int = 1, cols: int = 1) -> dict[str, int]:
    """0-based."""
    return {
        "sheetId": gid,
        "startRowIndex": row,
        "endRowIndex": row + rows,
        "startColumnIndex": col,
        "endColumnIndex": col + cols,
    }


def _text(size: int = 10, bold: bool = False, colour: str = INK) -> dict[str, Any]:
    return {
        "fontFamily": FONT,
        "fontSize": size,
        "bold": bold,
        "foregroundColor": _rgb(colour),
    }


def _border(colour: str) -> dict[str, Any]:
    return {"style": "SOLID", "color": _rgb(colour)}


def _paint(range_: dict[str, int], fmt: dict[str, Any]) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": range_,
            "cell": {"userEnteredFormat": fmt},
            "fields": "userEnteredFormat(" + ",".join(fmt) + ")",
        }
    }


def _size(gid: int, dimension: str, start: int, end: int, pixels: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": dimension, "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def _status_rule(gid: int, rows: dict[str, int], status: str) -> dict[str, Any]:
    fill, ink = _STATUS_COLOURS[status]
    return {
        "addConditionalFormatRule": {
            "index": 0,
            "rule": {
                "ranges": [rows],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": status}],
                    },
                    "format": {
                        "backgroundColor": _rgb(fill),
                        "textFormat": {"foregroundColor": _rgb(ink), "bold": True},
                    },
                },
            },
        }
    }


def schedule_requests(gid: int, separator: str) -> list[dict[str, Any]]:
    """Every format, size, rule and guard the tab has."""
    count = len(slots())
    first = FIRST_SLOT_ROW - 1
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": gid,
                    "gridProperties": {"frozenRowCount": 6, "hideGridlines": True},
                    "tabColor": _rgb(BLUE),
                },
                "fields": "gridProperties(frozenRowCount,hideGridlines),tabColor",
            }
        },
        _paint(
            {"sheetId": gid},
            {"textFormat": _text(), "verticalAlignment": "MIDDLE"},
        ),
        {"mergeCells": {"range": _range(gid, 1, 1, 1, 5), "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": _range(gid, 2, 1, 1, 7), "mergeType": "MERGE_ALL"}},
        _paint(_range(gid, 1, 1), {"textFormat": _text(16, True, TITLE)}),
        _paint(
            _range(gid, 1, 6),
            {"textFormat": _text(10, True, MUTED), "horizontalAlignment": "RIGHT"},
        ),
        _paint(
            _range(gid, 1, 7),
            {
                "textFormat": _text(12, True, TITLE),
                "horizontalAlignment": "CENTER",
                "backgroundColor": _rgb(CARD),
                "numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"},
                "borders": {
                    side: _border(CARD_EDGE) for side in ("top", "bottom", "left", "right")
                },
            },
        ),
        {
            "setDataValidation": {
                "range": _range(gid, 1, 7),
                "rule": {
                    "condition": {"type": "DATE_IS_VALID"},
                    "inputMessage": "Boshqa kunni ko'rish uchun sanani yozing. "
                    "Bugunga qaytish: =TODAY()",
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        },
        _paint(_range(gid, 2, 1), {"textFormat": _text(10, False, MUTED)}),
        # The cards: label above, figure below, one box each.
        _paint(
            _range(gid, 3, 1, 1, 7),
            {
                "textFormat": _text(9, True, CARD_LABEL),
                "horizontalAlignment": "CENTER",
                "backgroundColor": _rgb(CARD),
                "borders": {
                    "top": _border(CARD_EDGE),
                    "bottom": _border(CARD_INNER),
                    "left": _border(CARD_EDGE),
                    "right": _border(CARD_EDGE),
                },
            },
        ),
        _size(gid, "ROWS", 0, 1, 20),
        _size(gid, "ROWS", 1, 2, 45),
        _size(gid, "ROWS", 2, 3, 22),
        _size(gid, "ROWS", 3, 4, 26),
        _size(gid, "ROWS", 4, 5, 38),
        _size(gid, "ROWS", 5, 6, 35),
        _size(gid, "ROWS", first, first + count, 35),
    ]
    for index, (_, colour) in enumerate(CARDS):
        requests.append(
            _paint(
                _range(gid, 4, 1 + index),
                {
                    "textFormat": _text(16, True, colour),
                    "horizontalAlignment": "CENTER",
                    "backgroundColor": _rgb(CARD),
                    "numberFormat": (
                        {"type": "PERCENT", "pattern": "0%"}
                        if index == len(CARDS) - 1
                        else {"type": "NUMBER", "pattern": "#,##0"}
                    ),
                    "borders": {
                        "top": _border(CARD_INNER),
                        "bottom": _border(CARD_EDGE),
                        "left": _border(CARD_EDGE),
                        "right": _border(CARD_EDGE),
                    },
                },
            )
        )
    requests.append(
        _paint(
            _range(gid, 5, 1, 1, 7),
            {
                "backgroundColor": _rgb(BLUE),
                "textFormat": _text(10, True, "#FFFFFF"),
                "horizontalAlignment": "LEFT",
                "padding": {"left": 8, "right": 8},
            },
        )
    )
    for col in (1, 3, 7):
        requests.append(_paint(_range(gid, 5, col), {"horizontalAlignment": "CENTER"}))
    for offset in range(count):
        row = first + offset
        fill = ROW_ALT if offset % 2 else "#FFFFFF"
        requests.append(
            _paint(
                _range(gid, row, 1, 1, 7),
                {
                    "backgroundColor": _rgb(fill),
                    "textFormat": _text(10, False, INK),
                    "padding": {"left": 8, "right": 8},
                    "wrapStrategy": "CLIP",
                    "borders": {"top": _border(ROW_LINE), "bottom": _border(ROW_LINE)},
                },
            )
        )
    slots_range = _range(gid, first, 1, count, 1)
    requests += [
        _paint(
            slots_range,
            {
                "textFormat": _text(10, True, BLUE),
                "horizontalAlignment": "CENTER",
                "numberFormat": {"type": "TIME", "pattern": "h:mm"},
            },
        ),
        _paint(_range(gid, first, 3, count, 1), {"horizontalAlignment": "CENTER"}),
        _paint(_range(gid, first, 7, count, 1), {"horizontalAlignment": "CENTER"}),
        # The table's outer edge, as in the template.
        {
            "updateBorders": {
                "range": _range(gid, 5, 1, count + 1, 7),
                "left": _border(CARD_EDGE),
                "right": _border(CARD_EDGE),
                "bottom": _border(CARD_EDGE),
            }
        },
    ]
    for index, pixels in enumerate(WIDTHS):
        requests.append(_size(gid, "COLUMNS", index, index + 1, pixels))
    status_rows = _range(gid, first, 7, count, 1)
    requests += [_status_rule(gid, status_rows, status) for status in _STATUS_COLOURS]
    # The slot the clinic is in right now, when the chosen day is today.
    requests.append(
        {
            "addConditionalFormatRule": {
                "index": 0,
                "rule": {
                    "ranges": [_range(gid, first, 1, count, 6)],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [
                                {
                                    "userEnteredValue": localise(
                                        f"=AND($H$2=TODAY(),ROUND($B{FIRST_SLOT_ROW}"
                                        f"*{_per_day()})=INT(MOD(NOW(),1)*{_per_day()}))",
                                        separator,
                                    )
                                }
                            ],
                        },
                        "format": {"backgroundColor": _rgb(NOW_FILL)},
                    },
                },
            }
        }
    )
    # Formulas, not a place to type: a warning before one is overwritten.
    requests.append(
        {
            "addProtectedRange": {
                "protectedRange": {
                    "range": _range(gid, 2, 1, count + FIRST_SLOT_ROW - 3, 7),
                    "description": "Qabul jadvali Qabullar varag'idan o'zi to'ladi",
                    "warningOnly": True,
                }
            }
        }
    )
    return requests


def marker_requests(gid: int, rows: list[int]) -> list[dict[str, Any]]:
    """The "OY" cell of each month line, in the line's colours (0-based rows)."""
    return [
        _paint(
            {
                "sheetId": gid,
                "startRowIndex": row,
                "endRowIndex": row + 1,
                "startColumnIndex": 7,
                "endColumnIndex": 8,
            },
            {
                "backgroundColor": LINE_FILL,
                "textFormat": {"bold": True, "foregroundColor": LINE_INK},
                "verticalAlignment": "MIDDLE",
            },
        )
        for row in rows
    ]


class SheetDesign:
    def __init__(self, mirror: SheetsMirror | None = None) -> None:
        self._mirror = mirror or SheetsMirror()

    async def _batch(self, client: httpx.AsyncClient, requests: list[dict[str, Any]]) -> None:
        if requests:
            await self._mirror._call(client, "POST", ":batchUpdate", json={"requests": requests})

    async def apply(self) -> None:
        async with httpx.AsyncClient(timeout=60) as client:
            body = await self._mirror._call(
                client,
                "GET",
                "",
                params={
                    "fields": "properties.locale,sheets(properties(sheetId,title,index,"
                    "gridProperties),merges,conditionalFormats,protectedRanges,bandedRanges,"
                    "basicFilter)"
                },
            )
            separator = separator_for(body.get("properties", {}).get("locale"))
            sheets = {s["properties"]["title"]: s for s in body.get("sheets", [])}
            if APPOINTMENT_SHEET not in sheets:
                raise ValueError(f"no {APPOINTMENT_SHEET} sheet for the schedule to read")

            await self._schedule(client, sheets, separator)
            if REPORT_SHEET in sheets:
                await self._report(client, sheets[REPORT_SHEET])
            await self._markers(client, int(sheets[APPOINTMENT_SHEET]["properties"]["sheetId"]))
            await self._order(client)
            logger.info("sheet_design_applied")

    async def _schedule(
        self, client: httpx.AsyncClient, sheets: dict[str, Any], separator: str
    ) -> None:
        if SCHEDULE_SHEET in sheets:
            old = sheets[SCHEDULE_SHEET]
            gid = int(old["properties"]["sheetId"])
            reset: list[dict[str, Any]] = [
                {"deleteConditionalFormatRule": {"sheetId": gid, "index": 0}}
                for _ in old.get("conditionalFormats") or []
            ]
            reset += [{"unmergeCells": {"range": m}} for m in old.get("merges") or []]
            reset += [
                {"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}}
                for p in old.get("protectedRanges") or []
            ]
            reset.append(
                {
                    "updateCells": {
                        "range": {"sheetId": gid},
                        "fields": "userEnteredValue,userEnteredFormat,dataValidation",
                    }
                }
            )
            await self._batch(client, reset)
        else:
            reply = await self._mirror._call(
                client,
                "POST",
                ":batchUpdate",
                json={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": SCHEDULE_SHEET,
                                    "gridProperties": {
                                        "rowCount": FIRST_SLOT_ROW + len(slots()) + 10,
                                        "columnCount": len(WIDTHS) + 2,
                                    },
                                }
                            }
                        }
                    ]
                },
            )
            gid = int(reply["replies"][0]["addSheet"]["properties"]["sheetId"])
        await self._mirror._call(
            client,
            "PUT",
            f"/values/'{SCHEDULE_SHEET}'!A1",
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": schedule_values(separator)},
        )
        await self._batch(client, schedule_requests(gid, separator))

    async def _report(self, client: httpx.AsyncClient, sheet: dict[str, Any]) -> None:
        gid = int(sheet["properties"]["sheetId"])
        reset: list[dict[str, Any]] = [
            {"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}}
            for b in sheet.get("bandedRanges") or []
        ]
        if sheet.get("basicFilter"):
            reset.append({"clearBasicFilter": {"sheetId": gid}})
        reset.append({"updateCells": {"range": {"sheetId": gid}, "fields": "userEnteredFormat"}})
        await self._batch(client, reset)
        await self._batch(client, report_layout(gid))

    async def _markers(self, client: httpx.AsyncClient, gid: int) -> None:
        body = await self._mirror._call(
            client,
            "GET",
            f"/values/'{APPOINTMENT_SHEET}'!H:H",
            params={"majorDimension": "COLUMNS"},
        )
        column = (body.get("values") or [[]])[0]
        rows = [index for index, value in enumerate(column) if value == MARKER]
        await self._batch(client, marker_requests(gid, rows))

    async def _order(self, client: httpx.AsyncClient) -> None:
        body = await self._mirror._call(
            client, "GET", "", params={"fields": "sheets.properties(sheetId,title)"}
        )
        ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in body["sheets"]}
        await self._batch(
            client,
            [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": ids[title], "index": index},
                        "fields": "index",
                    }
                }
                for index, title in enumerate(t for t in TAB_ORDER if t in ids)
            ],
        )


if __name__ == "__main__":  # pragma: no cover - a command, run by hand
    logging.basicConfig(level=logging.INFO)
    asyncio.run(SheetDesign().apply())
    print("done")
