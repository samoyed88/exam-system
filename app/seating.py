"""座位表與鄰座作答相似度分析：找出「錯得一模一樣」或「同時作答同一題」多到不尋常的相鄰同學。

做法參考作弊統計分析常用的 Harpp–Hogan「相同錯誤答案」方法：
- 答對的題目大家答案本來就一樣，不能當證據；只看兩人都答錯、而且錯的答案完全相同的題數（EEIC）。
- 程度差的兩個人本來就常一起答錯，所以只在「兩人都答錯」的題目上算：用該題答錯的人當中各種錯法的
  比例，得到兩人碰巧錯成同一個答案的機率，再算「碰巧錯得這麼多題一樣」的機率（Poisson 二項分布）。
- 同步作答：兩人在 30 秒內給出同一題的相同答案。題目順序每人打散時，碰巧同步的機會很低；
  抄程度好的同學（幾乎沒有錯題）時，相同錯誤答案抓不到，主要靠這一項。機率用全班兩兩組合的同步題數
  配適負二項分布（比 Poisson 保守，容許「作答節奏相近」造成的偏高）來估計。
- 一次檢查很多組，總會有幾組碰巧偏高，所以機率要乘上檢查的組數（Bonferroni 校正）才判定。
結果只是統計上的線索，不能直接當作作弊證據，要配合監考觀察與學生說明。
"""
import json
import math
import re
from datetime import datetime

SYNC_SECONDS = 30
RELATION_TEXT = {"lr": "左右相鄰", "fb": "前後相鄰"}


def parse_seat_map(text: str) -> list[list[str]]:
    """每行一排，逗號或 Tab 分隔；空格子代表走道。可直接從 Excel 複製貼上。"""
    rows = []
    for line in text.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        rows.append([c.strip() for c in re.split(r"[,\t，]", line)])
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def neighbor_pairs(grid: list[list[str]]) -> list[tuple[tuple[int, int], tuple[int, int], str]]:
    """左右（同排相鄰、中間沒有走道）與前後（同欄相鄰排）的座位組合。"""
    pairs = []
    for r, row in enumerate(grid):
        for c, label in enumerate(row):
            if not label:
                continue
            if c + 1 < len(row) and row[c + 1]:
                pairs.append(((r, c), (r, c + 1), "lr"))
            if r + 1 < len(grid) and c < len(grid[r + 1]) and grid[r + 1][c]:
                pairs.append(((r, c), (r + 1, c), "fb"))
    return pairs


def normalize(qtype: str, response) -> str | None:
    """比對用：多選排序、填充題去空白與大小寫。沒作答回傳 None。"""
    if response is None or response == []:
        return None
    if qtype == "fill":
        blanks = [re.sub(r"\s+", "", str(s)).lower() for s in response]
        return None if not any(blanks) else json.dumps(blanks, ensure_ascii=False)
    if isinstance(response, list):
        return json.dumps(sorted(response))
    return json.dumps(response)


class Analyzer:
    """students：學號 → {題目 id: (標準化答案, 是否答錯, 作答時間)}，只放已交卷的學生。"""

    def __init__(self, students: dict[str, dict[int, tuple[str, bool, datetime | None]]]):
        self.students = students
        # 每題「兩個都答錯的人，碰巧錯成同一個答案」的機率：Σ(答錯者中各種錯法的比例²)
        self.p_same_wrong: dict[int, float] = {}
        wrong: dict[int, dict[str, int]] = {}
        for answers in students.values():
            for qid, (resp, is_wrong, _) in answers.items():
                if is_wrong:
                    wrong.setdefault(qid, {})
                    wrong[qid][resp] = wrong[qid].get(resp, 0) + 1
        for qid, counts in wrong.items():
            w = sum(counts.values())
            self.p_same_wrong[qid] = sum((c / w) ** 2 for c in counts.values())
        ids = sorted(students)
        counts = sorted(self.pair(a, b)["sync"] for i, a in enumerate(ids) for b in ids[i + 1:])
        # 用全部組合估計（不剔除偏高的組合）：少數作弊組合只會讓變異數略大、判定更保守
        normal = counts or [0]
        self.sync_mean = sum(normal) / len(normal)
        self.sync_var = sum((c - self.sync_mean) ** 2 for c in normal) / len(normal)

    def pair(self, a: str, b: str) -> dict:
        A, B = self.students[a], self.students[b]
        common = [q for q in A if q in B]
        same = eeic = diff = sync = 0
        probs = []  # 兩人都答錯的題目，各自碰巧錯成同一個答案的機率
        for q in common:
            (ra, wa, ta), (rb, wb, tb) = A[q], B[q]
            if wa and wb:
                probs.append(self.p_same_wrong.get(q, 0))
            if ra == rb:
                same += 1
                if wa and wb:
                    eeic += 1
                if ta and tb and abs((ta - tb).total_seconds()) <= SYNC_SECONDS:
                    sync += 1
            else:
                diff += 1
        return {"common": len(common), "same": same, "eeic": eeic, "diff": diff, "sync": sync,
                "both_wrong": len(probs), "expected": round(sum(probs), 2),
                "p_eeic": tail_probability(probs, eeic),
                "ratio": round(eeic / diff, 2) if diff else (None if not eeic else 99.0)}

    def judge(self, a: str, b: str, tests: int) -> dict:
        """tests：這一輪總共檢查幾組，用來校正「檢查越多組，越容易碰巧有一組偏高」。"""
        m = self.pair(a, b)
        p_sync = count_tail(m["sync"], self.sync_mean, self.sync_var)
        # 兩項檢查取較小的機率 ×2，再乘上檢查組數
        chance = min(1.0, min(m["p_eeic"], p_sync) * 2 * max(tests, 1))
        level = 2 if chance < .01 else 1 if chance < .05 else 0
        reasons = []
        if m["p_eeic"] * 2 * tests < .05:
            reasons.append(f"兩人都答錯的 {m['both_wrong']} 題中，有 {m['eeic']} 題錯成同一個答案"
                           f"（碰巧的話約 {m['expected']} 題）")
        if p_sync * 2 * tests < .05:
            reasons.append(f"有 {m['sync']} 題在 {SYNC_SECONDS} 秒內給出相同答案"
                           f"（全班兩兩平均 {self.sync_mean:.1f} 題）")
        return {**m, "p_sync": round(p_sync, 5), "chance": chance, "level": level, "reasons": reasons}


def count_tail(k: int, mean: float, var: float) -> float:
    """P(X ≥ k)：變異數大於平均時用負二項分布，否則用 Poisson。平均太小時以 0.2 為下限，避免三兩題就判定。"""
    if k <= 0:
        return 1.0
    mean = max(mean, .2)
    if var > mean * 1.05:
        r, q = mean * mean / (var - mean), mean / var
        pmf, below = q ** r, 0.0
        for j in range(k):
            below += pmf
            pmf *= (j + r) / (j + 1) * (1 - q)
    else:
        pmf, below = math.exp(-mean), 0.0
        for j in range(k):
            below += pmf
            pmf *= mean / (j + 1)
    return max(0.0, 1.0 - below)


def tail_probability(probs: list[float], k: int) -> float:
    """Poisson 二項分布：各題獨立、機率為 probs 時，至少 k 題碰巧一樣的機率。"""
    if k <= 0:
        return 1.0
    dist = [1.0]  # dist[j] = 恰好 j 題一樣的機率
    for p in probs:
        nxt = [0.0] * (len(dist) + 1)
        for j, v in enumerate(dist):
            nxt[j] += v * (1 - p)
            nxt[j + 1] += v * p
        dist = nxt
    return max(0.0, min(1.0, sum(dist[k:])))
