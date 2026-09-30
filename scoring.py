import math
from statistics import mean, pstdev


def _safe_float(v, default=None):
    try:
        x = float(v)
        if math.isfinite(x) and x > 0:
            return x
    except (TypeError, ValueError):
        pass
    return default


def _norm(values):
    """0-100へ正規化。極端な1頭だけで100になるのを少し抑える。"""
    if not values:
        return []

    lo = min(values)
    hi = max(values)

    if hi <= lo:
        return [50.0 for _ in values]

    return [
        max(0.0, min(100.0, 100.0 * (v - lo) / (hi - lo)))
        for v in values
    ]


def popularity_bonus(rank):
    """
    人気は主役にしない。
    4〜9番人気を「参考加点」するだけ。
    """
    try:
        rank = int(rank)
    except (TypeError, ValueError):
        return 0.0

    if 4 <= rank <= 9:
        return 100.0
    if rank in (3, 10):
        return 55.0
    if rank in (2, 11):
        return 20.0

    return 0.0


def odds_move_score(o15, o10, o5):
    """
    単勝オッズの時系列。
    15→10→5分で継続して買われている馬を評価。
    """
    o15 = _safe_float(o15)
    o10 = _safe_float(o10)
    o5 = _safe_float(o5)

    if not all((o15, o10, o5)):
        return 0.0

    total_drop = math.log(o15 / o5)

    d1 = math.log(o15 / o10)
    d2 = math.log(o10 / o5)

    consistency = 0.0
    if d1 > 0:
        consistency += 0.5
    if d2 > 0:
        consistency += 0.5

    raw = total_drop * 70.0 + consistency * 30.0

    return max(0.0, min(100.0, raw))


def frame_distortion_score(actual_odds, expected_odds):
    """
    枠連を最重要市場として評価。
    理論値より実際の枠連オッズが低いほど
    「その枠が相対的に買われている」と判定。
    """
    actual = _safe_float(actual_odds)
    expected = _safe_float(expected_odds)

    if not actual or not expected:
        return 0.0

    distortion = math.log(expected / actual)

    return max(0.0, min(100.0, 50.0 + distortion * 65.0))


def market_distortion(actual_prob, model_prob):
    """
    実際の市場確率と基準モデルとの差。
    D = ln(P_actual / P_model)
    """
    a = _safe_float(actual_prob)
    m = _safe_float(model_prob)

    if not a or not m:
        return 0.0

    d = math.log(a / m)

    return max(0.0, min(100.0, 50.0 + d * 55.0))


def signal_score(
    frame_score,
    win_move,
    quinella_score=0.0,
    exacta_score=0.0,
    trifecta_score=0.0,
    popularity=None,
):
    """
    ODDS SIGNAL Ver.7 基本配点

    枠連の歪み          35%
    単勝の時系列変化    25%
    馬連の歪み          15%
    馬単の歪み          10%
    三連単の歪み        10%
    人気帯              5%

    4〜9番人気は参考加点のみ。
    大穴・上位人気でも市場の歪みが強ければ候補になる。
    """

    frame_score = float(frame_score or 0)
    win_move = float(win_move or 0)
    quinella_score = float(quinella_score or 0)
    exacta_score = float(exacta_score or 0)
    trifecta_score = float(trifecta_score or 0)

    pop = popularity_bonus(popularity)

    score = (
        frame_score * 0.35
        + win_move * 0.25
        + quinella_score * 0.15
        + exacta_score * 0.10
        + trifecta_score * 0.10
        + pop * 0.05
    )

    return round(max(0.0, min(100.0, score)), 1)


def rank_signals(horses):
    """
    horses:
    [
      {
        "horse_no": 5,
        "popularity": 6,
        "frame_score": 82,
        "win_move": 74,
        "quinella_score": 65,
        "exacta_score": 58,
        "trifecta_score": 61
      }
    ]

    戻り値はSIGNALの高い順。
    """

    result = []

    for h in horses:
        x = dict(h)

        x["signal"] = signal_score(
            x.get("frame_score", 0),
            x.get("win_move", 0),
            x.get("quinella_score", 0),
            x.get("exacta_score", 0),
            x.get("trifecta_score", 0),
            x.get("popularity"),
        )

        result.append(x)

    result.sort(key=lambda x: x["signal"], reverse=True)

    return result
