"""本命チェック：3分前（またはその時点）の単勝が2.0倍未満の1番人気について、
他の券種での支持の強さ（base）だけで「信頼／注意／危険」を判定する。

基準は 2026-10-20 時点の34頭（来なかった10頭は base 平均 -0.57、来た24頭は -0.36）から決めて固定した。
結果を見たあとに決めた基準なので、当否はこの日より後に記録した判定だけで検証する（RULE_VERSION=1）。
自動の取捨（1番手から外す）は、検証で「危険」の3着内率がはっきり低いと確認できた時だけ有効になる。"""
import math

RULE_VERSION = 1
HEAVY = 2.0
DANGER_BASE = -0.45
CAUTION_BASE = -0.30
MIN_DANGER = 50        # 自動の取捨を有効にするのに必要な「危険」判定の頭数
MIN_GAP = 0.15         # 「危険」と他の3着内率の差がこれ以上
MAX_P = 0.05           # 片側検定の p 値がこれ未満

VERDICT_TEXT = {
    "危険": "馬連・3連複などの他の券種では、単勝ほど支持されていません",
    "注意": "他の券種での支持が、1倍台の人気馬としてはやや弱め",
    "信頼": "他の券種でも単勝と同じくらい支持されています",
}


def heavy_favorite(rows):
    """スナップショットの各馬の行から、単勝2.0倍未満の1番人気を返す（いなければ None）。"""
    rows = [r for r in rows or [] if r.get("odds")]
    if not rows:
        return None
    fav = min(rows, key=lambda r: (float(r["odds"]), int(r["horse"])))
    return fav if float(fav["odds"]) < HEAVY else None


def check(fav):
    """fav: {"odds","base"} -> {"verdict","reason"}"""
    b = float(fav.get("base") or 0.0)
    v = "危険" if b <= DANGER_BASE else "注意" if b <= CAUTION_BASE else "信頼"
    return {"verdict": v, "reason": VERDICT_TEXT[v], "base": round(b, 3)}


def check_rows(rows):
    fav = heavy_favorite(rows)
    if not fav:
        return None
    return {"horse": int(fav["horse"]), "odds": float(fav["odds"]), **check(fav)}


def _group(rows):
    out = []
    for v in ("信頼", "注意", "危険"):
        g = [r for r in rows if r["verdict"] == v]
        priced = [r for r in g if r.get("place_yen") is not None]
        out.append({"verdict": v, "horses": len(g), "placed": sum(1 for r in g if r["placed"]),
                    "place_rate": round(sum(1 for r in g if r["placed"]) / len(g), 4) if g else None,
                    "place_roi": round(sum(r["place_yen"] or 0 for r in priced) / (100 * len(priced)), 4) if priced else None})
    return out


def validation(rows):
    """rows: [{"verdict","placed","place_yen"}]（事前に記録した判定のみ）-> 集計と自動取捨の可否"""
    danger = [r for r in rows if r["verdict"] == "危険"]
    rest = [r for r in rows if r["verdict"] != "危険"]
    out = {"rule_version": RULE_VERSION, "horses": len(rows), "by_verdict": _group(rows), "enabled": False, "p_value": None, "gap": None,
           "need": max(0, MIN_DANGER - len(danger))}
    if danger and rest:
        p1 = sum(1 for r in danger if r["placed"]) / len(danger)
        p2 = sum(1 for r in rest if r["placed"]) / len(rest)
        pool = (sum(1 for r in rows if r["placed"])) / len(rows)
        se = math.sqrt(pool * (1 - pool) * (1 / len(danger) + 1 / len(rest)))
        z = (p2 - p1) / se if se > 0 else 0.0
        p = 0.5 * math.erfc(z / math.sqrt(2))
        out.update(gap=round(p2 - p1, 4), p_value=round(p, 4))
        out["enabled"] = len(danger) >= MIN_DANGER and (p2 - p1) >= MIN_GAP and p < MAX_P
    return out


def audit(rows):
    """過去分（結果を見た後に基準を決めたデータ）の参考集計。rows に verdict を付けて返す。"""
    for r in rows:
        r.update(check({"base": r.get("base3")}))
    total = len(rows)
    placed = sum(1 for r in rows if r["placed"])
    return {"heavy_favorites": total, "placed": placed, "place_rate": round(placed / total, 4) if total else None, "by_verdict": _group(rows)}


def demote(picks, fav_check, enabled):
    """自動取捨が有効で、1番手が「危険」の1倍台なら3番手へ下げる（候補からは外さない）。"""
    if not enabled or not fav_check or fav_check["verdict"] != "危険" or len(picks) < 3:
        return picks
    if int(picks[0]["horse"]) != fav_check["horse"]:
        return picks
    return picks[1:3] + [picks[0]] + picks[3:]
