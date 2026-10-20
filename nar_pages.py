"""Parsers for official NAR (keiba.go.jp) pages. Pure functions over BeautifulSoup documents.

Structures were taken from the live pages on 2026-10-20 (TodayRaceInfoTop, RaceList,
RaceMarkTable payouts, OddsTanFuku after the race). Nothing here touches the network or database.
"""
import re
from urllib.parse import parse_qs, urlparse

# Official k_babaCode -> display name (same names as the reservation form).
VENUE_NAMES = {"3": "帯広", "10": "盛岡", "11": "水沢", "18": "浦和", "19": "船橋", "20": "大井", "21": "川崎",
               "22": "金沢", "23": "笠松", "24": "名古屋", "27": "園田", "28": "姫路", "31": "高知", "32": "佐賀", "36": "門別"}
# The reservation form stores 佐賀 as "55" (translated back by qfor()); every other code is the official one.
_OFFICIAL_TO_STORED = {"32": "55"}
CANCEL_WORDS = ("取止", "中止", "取りやめ")
PAYOUT_TYPES = ("単勝", "複勝", "枠連複", "枠連単", "馬連複", "馬連単", "ワイド", "三連複", "三連単")
ORDERED_TYPES = ("枠連単", "馬連単", "三連単")


def _cells(tr):
    return [re.sub(r"\s+", " ", " ".join(x.stripped_strings)).strip() for x in tr.find_all(["td", "th"])]


def stored_baba(official_code):
    code = str(int(str(official_code)))
    return _OFFICIAL_TO_STORED.get(code, code)


def parse_venue_days(doc, dates):
    """(date 'YYYY-MM-DD', official code) pairs linked from TodayRaceInfoTop for the wanted dates."""
    want, out = set(dates), []
    for a in doc.find_all("a"):
        href = a.get("href") or ""
        if "RaceList" not in href:
            continue
        q = parse_qs(urlparse(href).query)
        d, code = (q.get("k_raceDate") or [""])[0], (q.get("k_babaCode") or [""])[0]
        m = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})", d)
        if not m or not code.isdigit():
            continue
        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if iso in want and (iso, str(int(code))) not in out:
            out.append((iso, str(int(code))))
    return out


def parse_race_list(doc):
    """Rows of a venue's 当日メニュー: [{race, time 'HH:MM' or None, changed, cancelled, name}]."""
    out = {}
    for table in doc.find_all("table"):
        header = None
        for tr in table.find_all("tr"):
            cells = _cells(tr)
            if "発走時刻" in cells and any(c.startswith("競走") for c in cells):
                header = cells
                continue
            if not header or not cells:
                continue
            m = re.fullmatch(r"(\d{1,2})R", cells[0])
            if not m:
                continue
            idx = header.index("発走時刻")
            t = cells[idx] if idx < len(cells) else ""
            tm = re.search(r"(\d{1,2}):(\d{2})", t)
            cancelled = any(w in c for c in cells for w in CANCEL_WORDS)
            chg = header.index("変更") if "変更" in header else None
            name_i = next((i for i, h in enumerate(header) if h.startswith("競走名")), None)
            out[int(m.group(1))] = {
                "race": int(m.group(1)),
                "time": f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else None,
                "changed": bool(chg is not None and chg < len(cells) and cells[chg]),
                "cancelled": cancelled,
                "name": cells[name_i] if name_i is not None and name_i < len(cells) else "",
            }
    return [out[k] for k in sorted(out)]


def _combo(text, kind):
    nums = [int(x) for x in re.findall(r"\d{1,2}", text)]
    need = 1 if kind in ("単勝", "複勝") else 3 if kind.startswith("三連") else 2
    if len(nums) != need:
        return None
    return tuple(nums) if kind in ORDERED_TYPES else tuple(sorted(nums))


def parse_payouts(doc):
    """Official refunds: {type: [{"combo": [..], "yen": int per 100円, "pop": int|None}]}.
    Handles rowspan continuation rows and dead heats (several payouts per type)."""
    out = {}
    for table in doc.find_all("table"):
        cur = None
        for tr in table.find_all("tr"):
            cells = _cells(tr)
            if not cells:
                continue
            if cells[0] in PAYOUT_TYPES:
                cur, rest = cells[0], cells[1:]
            elif cur and len(cells) >= 2 and re.search(r"[\d,]+円", cells[1]):
                rest = cells
            else:
                cur = None
                continue
            if len(rest) < 2:
                continue
            combo = _combo(rest[0], cur)
            ym = re.fullmatch(r"([\d,]+)円", rest[1])
            if combo is None or not ym:
                continue
            pm = re.match(r"(\d+)", rest[2]) if len(rest) > 2 else None
            entry = {"combo": list(combo), "yen": int(ym.group(1).replace(",", "")), "pop": int(pm.group(1)) if pm else None}
            if entry not in out.setdefault(cur, []):
                out[cur].append(entry)
    return out


def parse_win_place(doc):
    """Win and place odds from OddsTanFuku: {horse: {"win": float, "place_low": float, "place_high": float}}.
    After the race this page shows the final (確定) odds."""
    out = {}
    num = re.compile(r"\d+(?:\.\d+)?")
    for tr in doc.find_all("tr"):
        cells = _cells(tr)
        # The horse name is the first cell that is not a number; 枠 may be merged across rows.
        name_i = next((i for i, c in enumerate(cells) if c and not re.fullmatch(r"[\d.,\-（）() ]+", c)), None)
        if name_i is None or name_i < 1 or len(cells) < name_i + 4:
            continue
        if not re.fullmatch(r"\d{1,2}", cells[name_i - 1]):
            continue
        horse = int(cells[name_i - 1])
        w, lo, hi = cells[name_i + 1], cells[name_i + 2].rstrip("-").strip(), cells[name_i + 3]
        if not (num.fullmatch(w.replace(",", "")) and num.fullmatch(lo.replace(",", "")) and num.fullmatch(hi.replace(",", ""))):
            continue
        if 1 <= horse <= 18:
            out[horse] = {"win": float(w.replace(",", "")), "place_low": float(lo.replace(",", "")), "place_high": float(hi.replace(",", ""))}
    return out


def parse_today_grid(doc):
    """TodayRaceInfoTop's grid of today's races: {official code: {race: cell text}}.
    The grid is the only place that marks races 取止 (the venue race list keeps the time)."""
    out = {}
    for table in doc.find_all("table"):
        rows = [_cells(tr) for tr in table.find_all("tr")]
        head = next((r for r in rows if sum(bool(re.fullmatch(r"\d{1,2}R", c)) for c in r) >= 3), None)
        if not head:
            continue
        cols = {i: int(c[:-1]) for i, c in enumerate(head) if re.fullmatch(r"\d{1,2}R", c)}
        for r in rows:
            if r is head or not r:
                continue
            code = next((k for k, v in VENUE_NAMES.items() if r[0].startswith(v)), None)
            if code:
                out[code] = {race: r[i] for i, race in cols.items() if i < len(r) and r[i]}
    return out


def cancelled_races(grid, code):
    return {race for race, cell in grid.get(str(int(code)), {}).items() if any(w in cell for w in CANCEL_WORDS)}
