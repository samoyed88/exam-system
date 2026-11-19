"""後台操作日誌：誰（哪次登入、哪個 IP 與裝置）在什麼時候做了什麼、改了哪些內容。

後台寫入類的請求（新增、修改、刪除、匯入、解鎖…）、後台登入與成績下載，由中介層一律記錄，
處理函式不必記得要寫；處理函式可以用 note() 補上人看得懂的摘要與修改前後的內容。
後台只有一組密碼，無法分辨是哪個人，但每次登入有自己的工作階段代號，配合 IP 與裝置可以追到是哪一台電腦。
"""
import contextvars
import hashlib
import json
from datetime import datetime, timezone

CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("audit", default=None)

ACTIONS = {
    "admin_login": "後台登入",
    "create_exam": "新增考試", "update_exam": "修改考試", "delete_exam": "刪除考試",
    "create_question": "新增題目", "update_question": "修改題目", "delete_question": "刪除題目",
    "import_questions": "匯入題目", "accept_fill": "填充題加入可接受答案",
    "upsert_student": "新增／修改學生", "delete_student": "刪除學生", "reset_student_password": "清除學生密碼",
    "import_students": "匯入名單", "import_students_xlsx": "匯入名單",
    "create_class": "新增班級", "rename_class": "班級改名", "delete_class": "刪除班級",
    "save_class_seats": "修改班級座位表", "import_class_seats": "匯入班級座位表",
    "save_seats": "修改考試座位表", "import_seats": "匯入考試座位表",
    "set_grade_order": "上傳成績總表順序", "clear_grade_order": "清除成績總表順序",
    "force_submit": "替學生強制交卷", "reset_attempt": "刪除作答（讓學生重考）", "unlock_attempt": "解鎖作答",
    "results_csv": "下載成績 CSV", "exam_items_csv": "下載試題分析 CSV", "export_grades_xlsx": "匯出成績 Excel",
}


def session_code(token: str) -> str:
    """登入憑證的短代號：同一次登入的操作代號相同，不會在日誌裡留下憑證本身。"""
    return hashlib.sha256(token.encode()).hexdigest()[:6] if token else ""


def note(summary: str, detail: dict | None = None, target: str = "", session: str = "") -> None:
    """在處理函式裡補充這次操作的摘要；不在後台請求中（例如測試或指令）時不做事。"""
    ctx = CURRENT.get()
    if ctx is None:
        return
    ctx["summary"] = summary
    if detail:
        ctx["detail"] = detail
    if target:
        ctx["target"] = target
    if session:
        ctx["session"] = session


def changes(before: dict, after: dict, labels: dict[str, str] | None = None) -> dict:
    """只留下有變動的欄位：{欄位: [修改前, 修改後]}。"""
    labels = labels or {}
    return {labels.get(k, k): [before.get(k), after.get(k)]
            for k in after if before.get(k) != after.get(k)}


def write(db, action: str, summary: str = "", detail: dict | None = None, target: str = "",
          actor: str = "系統指令", ip: str = "", device: str = "", session: str = "", status: int = 200,
          method: str = "", path: str = "") -> None:
    db.execute(
        "INSERT INTO admin_log (at, actor, ip, device, session, action, target, summary, detail, status, method, path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), actor, ip, device[:200], session, action, target[:200],
         summary[:500], json.dumps(detail or {}, ensure_ascii=False, default=str)[:20000], status, method, path))


def device_name(user_agent: str) -> str:
    """User-Agent 簡化成「Chrome／macOS」這類好讀的字。"""
    ua = user_agent or ""
    browser = next((b for k, b in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                   ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "其他瀏覽器")
    system = next((s for k, s in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                  ("Windows", "Windows"), ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"),
                                  ("Linux", "Linux")) if k in ua), "")
    if not ua:
        return ""
    return browser + ("／" + system if system else "")
