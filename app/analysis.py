"""試題分析（仿 Moodle 測驗結構分析）：答對率、標準差、隨機猜測分數、預期與有效權重、鑑別指數、鑑別效率，
以及整份測驗的內部一致性信度（Cronbach's α）。

定義（與 Moodle quiz statistics 相同）：
- 答對率：這題平均得分 ÷ 配分。
- 標準差：這題得分的樣本標準差 ÷ 配分。
- 鑑別指數：這題得分與「其餘題目總分」的相關係數 ×100。負值代表程度好的學生反而答錯，題目或答案可能有問題。
- 鑑別效率：這題與其餘總分的共變異數 ÷ 在同樣分布下可能的最大共變異數 ×100。
- 有效權重：各題與總分共變異數的平方根占全部的比例；預期權重：配分 ÷ 總分。
"""
import itertools
import math
import re
import statistics


def _cov(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)


def random_guess(qtype: str, num_options: int, grade_fn) -> float | None:
    """隨機作答的期望得分比例。單選：1／選項數；多選：隨機勾任意非空組合的平均；填充：0。"""
    if qtype == "fill":
        return 0.0
    if not num_options:
        return None
    if qtype == "single":
        return 1 / num_options
    subsets = [list(c) for k in range(1, num_options + 1) for c in itertools.combinations(range(num_options), k)]
    return sum(grade_fn(s) for s in subsets) / len(subsets)


def item_name(tags: str, stem: str) -> str:
    """Moodle 的題目名稱（例如 CH07.1-28(L1)）存在標籤裡；沒有就用題幹開頭。"""
    for tag in (t.strip() for t in tags.split(",")):
        if re.match(r"^CH\S*\d", tag, re.I):
            return tag
    return stem[:24] + ("…" if len(stem) > 24 else "")


def item_analysis(items: list[dict], scores: list[dict[int, float]]) -> dict:
    """items：[{id, points, guess}]（考卷順序）；scores：每位已交卷學生 {題目 id: 得分}，沒作答算 0。"""
    n = len(scores)
    total_points = sum(i["points"] for i in items) or 1
    matrix = {i["id"]: [s.get(i["id"], 0.0) or 0.0 for s in scores] for i in items}
    totals = [sum(matrix[i["id"]][k] for i in items) for k in range(n)]
    out = []
    root_cov = {}
    for it in items:
        x, pts = matrix[it["id"]], it["points"] or 1
        rest = [t - v for t, v in zip(totals, x)]
        row = {"id": it["id"], "count": n, "intended": it["points"] / total_points,
               "guess": it.get("guess"), "facility": None, "sd": None,
               "discrimination": None, "efficiency": None, "effective": None}
        if n:
            row["facility"] = sum(x) / n / pts
        if n >= 2:
            row["sd"] = statistics.stdev(x) / pts
            vx, vr = _cov(x, x), _cov(rest, rest)
            c = _cov(x, rest)
            if vx > 0 and vr > 0:
                row["discrimination"] = c / math.sqrt(vx * vr)
            best = _cov(sorted(x), sorted(rest))
            if best > 0:
                row["efficiency"] = c / best
            ct = _cov(x, totals)
            if ct > 0:
                root_cov[it["id"]] = math.sqrt(ct)
        out.append(row)
    sum_root = sum(root_cov.values())
    for row in out:
        if row["id"] in root_cov and sum_root:
            row["effective"] = root_cov[row["id"]] / sum_root
    alpha = None
    k = len(items)
    if n >= 2 and k >= 2:
        var_total = _cov(totals, totals)
        if var_total > 0:
            alpha = k / (k - 1) * (1 - sum(_cov(matrix[i["id"]], matrix[i["id"]]) for i in items) / var_total)
    return {"count": n, "alpha": alpha, "items": out}


def flags(row: dict) -> list[str]:
    """需要檢查的題目：鑑別度為負或太低、太難或太簡單。"""
    out = []
    d, f = row["discrimination"], row["facility"]
    if d is not None and d < 0:
        out.append("鑑別指數為負：程度好的學生反而答錯，請檢查題目與答案")
    elif d is not None and d < .2:
        out.append("鑑別指數偏低（< 20%）")
    if f is not None and f >= .9:
        out.append("太簡單（答對率 ≥ 90%）")
    elif f is not None and row["guess"] is not None and f <= row["guess"]:
        out.append("答對率不高於隨機猜測")
    elif f is not None and f <= .1:
        out.append("太難（答對率 ≤ 10%）")
    return out
