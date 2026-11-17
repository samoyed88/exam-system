"""評分規則：單選、多選（全對或學測式部分給分）、填充（每格獨立、寬鬆比對）。"""
import re
import unicodedata
from fractions import Fraction


def normalize(text: str) -> str:
    # NFKC 會把全形英數、全形空白轉半形；再把連續空白壓成一個並轉小寫
    text = unicodedata.normalize("NFKC", str(text)).strip()
    return re.sub(r"\s+", " ", text).casefold()


def as_number(text: str):
    try:
        return Fraction(text.replace(" ", ""))
    except (ValueError, ZeroDivisionError):
        return None


def fill_match(response: str, accepted: list[str]) -> bool:
    got = normalize(response)
    if not got:
        return False
    got_num = as_number(got)
    for ans in accepted:
        want = normalize(ans)
        if got == want:
            return True
        want_num = as_number(want)
        if got_num is not None and want_num is not None and got_num == want_num:
            return True
    return False


def grade(qtype: str, answer, response, points: float,
          multi_mode: str = "all", num_options: int = 0) -> float:
    """回傳這題得分；response 為 None 代表沒作答。"""
    if response is None:
        return 0.0
    if qtype == "single":
        return float(points) if response == answer[0] else 0.0
    if qtype == "multi":
        if not isinstance(response, list):
            return 0.0
        picked, correct = set(response), set(answer)
        if multi_mode == "all":
            return float(points) if picked == correct else 0.0
        # 學測規則：n 個選項錯 k 個得 (n - 2k) / n，最低 0
        if not picked:
            return 0.0
        n = max(num_options, len(picked | correct), 1)
        return round(points * max(0.0, (n - 2 * len(picked ^ correct)) / n), 4)
    if qtype == "fill":
        if not isinstance(response, list) or not answer:
            return 0.0
        per_blank = points / len(answer)
        hits = sum(
            1 for i, accepted in enumerate(answer)
            if i < len(response) and fill_match(response[i], accepted)
        )
        return round(per_blank * hits, 4)
    return 0.0
