"""Lightweight zero-dependency Street Fighter III: 3rd Strike character name lookups."""

from __future__ import annotations

CHAR_NAMES: dict[int, str] = {
    0: "gill",
    1: "alex",
    2: "ryu",
    3: "yun",
    4: "dudley",
    5: "necro",
    6: "hugo",
    7: "ibuki",
    8: "elena",
    9: "oro",
    10: "yang",
    11: "ken",
    12: "sean",
    13: "urien",
    14: "gouki",
    15: "gill2",
    16: "chunli",
    17: "makoto",
    18: "q",
    19: "twelve",
    20: "remy",
}

PRETTY_CHAR_NAMES: dict[int, str] = {
    0: "Gill",
    1: "Alex",
    2: "Ryu",
    3: "Yun",
    4: "Dudley",
    5: "Necro",
    6: "Hugo",
    7: "Ibuki",
    8: "Elena",
    9: "Oro",
    10: "Yang",
    11: "Ken",
    12: "Sean",
    13: "Urien",
    14: "Akuma",
    15: "Gill",
    16: "Chun-Li",
    17: "Makoto",
    18: "Q",
    19: "Twelve",
    20: "Remy",
}


def pretty_char(char_id: int) -> str:
    cid = int(char_id)
    return PRETTY_CHAR_NAMES.get(cid, CHAR_NAMES.get(cid, f"Char#{cid}"))
