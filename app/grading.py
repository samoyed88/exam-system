"""評分規則：單選、多選（全對、學測式部分給分、或依選項比重可倒扣）、填充（每格獨立、寬鬆比對）。"""
import re
import unicodedata
from fractions import Fraction


def normalize(text: str, fold: bool = True) -> str:
    # NFKC 會把全形英數、全形空白轉半形；連續空白壓成一個，fold 時轉小寫。
    # 符號旁邊的空白不影響程式碼的意思（students[0] = 5 和 students[0]=5 相同），一律去掉；
    # 兩個英數字之間的空白要保留（double students 不能變成 doublestudents）
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text)).strip())
    text = re.sub(r"(?<=[^\w\s])\s+|\s+(?=[^\w\s])", "", text)
    return text.casefold() if fold else text


# 只認一般小數與分數。Fraction 也吃 1e999999999 這種指數寫法，展開要算好幾小時，
# 交卷評分時又握著資料庫寫入鎖，一個學生就能讓全場卡住
NUMBER = re.compile(r"[-+]?(\d{1,30}(\.\d{0,30})?|\.\d{1,30})(/\d{1,30})?")


def as_number(text: str):
    text = text.replace(" ", "")
    if not NUMBER.fullmatch(text):
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def fill_match(response: str, accepted: list[str], case_sensitive: bool = False) -> bool:
    got = normalize(response, not case_sensitive)
    if not got:
        return False
    got_num = as_number(got)
    for ans in accepted:
        want = normalize(ans, not case_sensitive)
        if got == want:
            return True
        want_num = as_number(want)
        if got_num is not None and want_num is not None and got_num == want_num:
            return True
    return False


def grade(qtype: str, answer, response, points: float,
          multi_mode: str = "all", num_options: int = 0, weights: list[float] | None = None,
          case_sensitive: bool = False) -> float:
    """回傳這題得分；response 為 None 代表沒作答。"""
    if response is None:
        return 0.0
    if qtype == "single":
        return float(points) if response == answer[0] else 0.0
    if qtype == "multi":
        if not isinstance(response, list):
            return 0.0
        picked, correct = set(response), set(answer)
        if weights:
            # Moodle 規則：選到的選項比重加總（錯的選項是負數，會倒扣），介於 0 到滿分
            total = sum(weights[i] for i in picked if 0 <= i < len(weights))
            return round(points * max(0.0, min(1.0, total)), 4)
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
        # 題目已經給了分號，學生多打的分號算錯（老師決定）；只放寬空格
        hits = sum(
            1 for i, accepted in enumerate(answer)
            if i < len(response) and fill_match(response[i], accepted, case_sensitive)
        )
        return round(per_blank * hits, 4)
    return 0.0
