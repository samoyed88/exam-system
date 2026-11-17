"""解析學生名單：CSV 或學校成績登記表 xlsx，自動找含「學號」「姓名」的表頭列。"""
import base64
import csv
import io
import re

SID_HEADER = re.compile(r"學號|student_?id|stud", re.I)
NAME_HEADER = re.compile(r"姓名|^name$", re.I)
CLASS_HEADER = re.compile(r"^(班級|class_?name|class)$", re.I)


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # Excel 把學號存成數字時會變 11201.0
    return str(v).strip()


def _first_part(header: str) -> str:
    # 「姓名  Name」「班級 | Class」這類中英並列表頭，只取第一段比對
    return re.split(r"[\s|]+", header.strip())[0] if header.strip() else ""


def parse_rows(rows: list[list]) -> list[tuple[str, str, str]]:
    """回傳 [(學號, 姓名, 班級)]；找不到表頭就丟 ValueError。"""
    for h, row in enumerate(rows[:30]):
        cells = [_cell(c) for c in row]
        sid_col = next((i for i, c in enumerate(cells) if SID_HEADER.search(c)), None)
        name_col = next((i for i, c in enumerate(cells)
                         if NAME_HEADER.search(c) or NAME_HEADER.search(_first_part(c))), None)
        if sid_col is None or name_col is None:
            continue
        class_col = next((i for i, c in enumerate(cells)
                          if CLASS_HEADER.search(c) or CLASS_HEADER.search(_first_part(c))), None)
        out = []
        for r in rows[h + 1:]:
            cells = [_cell(c) for c in r]
            get = lambda i: cells[i] if i is not None and i < len(cells) else ""
            sid, name = get(sid_col), get(name_col)
            if sid and name:
                out.append((sid, name, get(class_col)))
        return out
    raise ValueError("找不到含「學號」和「姓名」的表頭列")


def parse_csv(text: str) -> list[tuple[str, str, str]]:
    return parse_rows(list(csv.reader(io.StringIO(text.lstrip("﻿")))))


def parse_xlsx(data_base64: str) -> list[tuple[str, str, str]]:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(data_base64)),
                                    read_only=True, data_only=True)
    except Exception:
        raise ValueError("無法讀取這個 Excel 檔")
    # 成績登記表常有很多工作表，用第一個找得到名單表頭的
    for ws in wb.worksheets:
        try:
            rows = parse_rows([list(r) for r in ws.iter_rows(max_row=500, values_only=True)])
        except ValueError:
            continue
        if rows:
            return rows
    raise ValueError("每個工作表都找不到含「學號」和「姓名」的表頭列")
