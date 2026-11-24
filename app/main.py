"""考試系統後端：學生以學號進場作答，老師後台管理名單、題庫、考試與成績。"""
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import random
import re
import sqlite3
import statistics
from contextlib import asynccontextmanager
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import URL, Headers, MutableHeaders
from pydantic import BaseModel

from .auth import (
    ADMIN_TTL, admin_account_token, admin_subject, check_admin_password, client_ip, current_student,
    hash_password, ip_allowed, login_locked, make_token, record_failure, require_admin, require_admin_self,
    student_token, token_actor, verify_password,
)
from .db import connect, get_db, init_db
from .grading import fill_match, grade, statement_blanks, strip_statement_semicolon
from . import audit
from .analysis import flags as item_flags
from .analysis import item_analysis, item_name, random_guess
from .roster import parse_csv, parse_xlsx
from .seating import (
    RELATION_TEXT, Analyzer, clean_sheet, neighbor_pairs, normalize, parse_seat_map, sheet_rows_from_file,
    split_label,
)

TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Taipei"))
GRACE_SECONDS = 30  # 網路延遲寬限：時間到後 30 秒內送達的作答仍收
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

AUTO_SUBMIT_INTERVAL = 5  # 秒


@asynccontextmanager
async def lifespan(_app):
    init_db()
    task = asyncio.create_task(auto_submit_loop())
    yield
    task.cancel()


app = FastAPI(title="考試系統", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

MAX_BODY = 64 * 1024             # 學生端請求：一題答案用不到這麼多
MAX_ADMIN_BODY = 20 * 1024 * 1024  # 後台匯入 xlsx（base64）
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'; object-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=15552000",
}


class SecurityMiddleware:
    """純 ASGI 寫法：BaseHTTPMiddleware（@app.middleware）在 100 人同時作答時會讓存檔延遲變成好幾倍。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path = scope["path"]
        # 經 Cloudflare 進來的 http 連線轉到 https，避免學號、token 明文傳送
        if os.getenv("IP_HEADER", "").lower() == "cf" and '"http"' in headers.get("cf-visitor", ""):
            url = str(URL(scope=scope).replace(scheme="https"))
            return await RedirectResponse(url, status_code=308)(scope, receive, send)
        limit = MAX_ADMIN_BODY if path.startswith("/api/admin/") else MAX_BODY
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > limit:
            return await JSONResponse({"detail": "資料太大"}, status_code=413)(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out.update(SECURITY_HEADERS)
                if path.startswith("/api/"):
                    out["Cache-Control"] = "no-store"  # 考場電腦共用，成績與作答不要留在快取
                elif "Cache-Control" not in out:
                    # 頁面引用的 CSS/JS 帶 ?v=內容雜湊，可以放心長期快取；其他靜態檔每次向伺服器確認
                    versioned = scope.get("query_string", b"").startswith(b"v=")
                    out["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AuditMiddleware:
    """後台寫入、登入與成績下載一律記進 admin_log，處理函式用 audit.note() 補充摘要。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path, method = scope.get("path", ""), scope.get("method", "")
        if scope["type"] != "http" or not path.startswith("/api/admin/") or \
                (method == "GET" and not path.endswith((".csv", ".xlsx"))):
            return await self.app(scope, receive, send)
        ctx = {}
        status = {"code": 500}

        async def send_status(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        token = audit.CURRENT.set(ctx)
        try:
            await self.app(scope, receive, send_status)
        finally:
            audit.CURRENT.reset(token)
            endpoint = getattr(scope.get("endpoint"), "__name__", "")
            if status["code"] == 401 and endpoint != "admin_login":
                return  # 沒登入的請求不記，避免被灌爆
            headers = Headers(scope=scope)
            bearer = headers.get("authorization", "").removeprefix("Bearer ").strip()
            row = dict(actor=ctx.get("actor") or token_actor(bearer) or "後台", ip=client_ip(Request(scope)),
                       device=audit.device_name(headers.get("user-agent", "")),
                       session=ctx.get("session") or audit.session_code(bearer),
                       action=audit.ACTIONS.get(endpoint, endpoint or path), target=ctx.get("target", ""),
                       summary=ctx.get("summary", ""), detail=ctx.get("detail"), status=status["code"],
                       method=method, path=path)
            await asyncio.to_thread(_write_audit, row)


def _write_audit(row: dict) -> None:
    conn = connect()
    try:
        audit.write(conn, **row)
    except Exception as e:  # 日誌寫失敗不能影響操作本身
        print(f"audit failed: {e!r}", flush=True)
    finally:
        conn.close()


app.add_middleware(AuditMiddleware)
app.add_middleware(SecurityMiddleware)


async def auto_submit_loop():
    """時間到由伺服器用已存答案代交，不依賴學生的瀏覽器還開著或網路還通。"""
    while True:
        await asyncio.sleep(AUTO_SUBMIT_INTERVAL)
        try:
            await asyncio.to_thread(_auto_submit_once)
        except Exception as e:  # 單次失敗不能讓迴圈停掉
            print(f"auto submit failed: {e!r}", flush=True)


def _auto_submit_once() -> None:
    conn = connect()
    try:
        finalize_expired(conn)
    finally:
        conn.close()


# ── 共用工具 ──


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(text: str) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def class_list(text: str) -> list[str]:
    return [c.strip() for c in text.split(",") if c.strip()]


def exam_or_404(db, exam_id: int) -> sqlite3.Row:
    exam = db.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if not exam:
        raise HTTPException(404, "找不到這場考試")
    return exam


def exam_questions(db, exam_id: int) -> list[sqlite3.Row]:
    # points 用這場考試設定的配分（沒設定就用題庫預設），評分、統計、學生畫面都從這裡取
    return db.execute(
        "SELECT q.id, q.type, q.stem, q.code, q.options, q.answer, q.explanation, "
        "COALESCE(eq.points, q.points) AS points, q.points AS bank_points, q.multi_mode, q.tags, q.weights, "
        "q.case_sensitive "
        "FROM exam_questions eq JOIN questions q ON q.id = eq.question_id "
        "WHERE eq.exam_id = ? ORDER BY eq.position", (exam_id,)
    ).fetchall()


def score_answer(q: sqlite3.Row, response) -> float:
    return grade(q["type"], json.loads(q["answer"]), response, q["points"],
                 q["multi_mode"], len(json.loads(q["options"])),
                 json.loads(q["weights"]) if q["weights"] else None, bool(q["case_sensitive"]),
                 statement_blanks(q["code"], len(json.loads(q["answer"]))) if q["type"] == "fill" else None)


def blank_ok(q: sqlite3.Row, i: int, text: str) -> bool:
    """填充題第 i 格的寫法算不算對，規則與評分相同（顯示與統計用）。"""
    key = json.loads(q["answer"])
    if i >= len(key):
        return False
    if statement_blanks(q["code"], len(key))[i]:
        text = strip_statement_semicolon(text)
    return fill_match(text, key[i], bool(q["case_sensitive"]))


def log_event(db, attempt_id: int, kind: str, question_id: int | None = None,
              detail: str = "", at: datetime | None = None) -> None:
    db.execute("INSERT INTO attempt_log (attempt_id, at, kind, question_id, detail) "
               "VALUES (?, ?, ?, ?, ?)",
               (attempt_id, (at or now()).isoformat(), kind, question_id, detail))


def finalize(db, attempt_id: int, kind: str = "auto_submit") -> None:
    """交卷並評分；已交卷就不動，可重複呼叫。kind 記在作答紀錄：submit / auto_submit / force_submit。"""
    attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt or attempt["submitted_at"]:
        return
    total = max_total = 0.0
    db.execute("BEGIN IMMEDIATE")
    try:
        # 兩個 worker 可能同時代交同一份；拿到寫入鎖後再確認一次
        if db.execute("SELECT submitted_at FROM attempts WHERE id = ?",
                      (attempt_id,)).fetchone()["submitted_at"]:
            db.execute("ROLLBACK")
            return
        saved = {r["question_id"]: json.loads(r["response"]) for r in db.execute(
            "SELECT question_id, response FROM answers WHERE attempt_id = ?", (attempt_id,))}
        for q in exam_questions(db, attempt["exam_id"]):
            max_total += q["points"]
            if q["id"] in saved:
                s = score_answer(q, saved[q["id"]])
                total += s
                db.execute("UPDATE answers SET score = ? WHERE attempt_id = ? AND question_id = ?",
                           (s, attempt_id, q["id"]))
        db.execute(
            "UPDATE attempts SET submitted_at = ?, score = ?, max_score = ? "
            "WHERE id = ? AND submitted_at IS NULL",
            (now().isoformat(), round(total, 2), round(max_total, 2), attempt_id))
        log_event(db, attempt_id, kind)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def finalize_expired(db, exam_id: int | None = None) -> None:
    # 用時間物件比較而不是比字串：舊資料的 deadline 可能帶 +08:00，跟 UTC 字串比會差 8 小時
    cutoff = now() - timedelta(seconds=GRACE_SECONDS)
    sql = "SELECT id, deadline FROM attempts WHERE submitted_at IS NULL"
    args: list = []
    if exam_id is not None:
        sql += " AND exam_id = ?"
        args.append(exam_id)
    for row in db.execute(sql, args).fetchall():
        if datetime.fromisoformat(row["deadline"]) < cutoff:
            finalize(db, row["id"])


def regrade(db, attempt_id: int) -> None:
    attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt or not attempt["submitted_at"]:
        return
    qs = {q["id"]: q for q in exam_questions(db, attempt["exam_id"])}
    total = 0.0
    for r in db.execute("SELECT question_id, response FROM answers WHERE attempt_id = ?",
                        (attempt_id,)).fetchall():
        q = qs.get(r["question_id"])
        if q:
            s = score_answer(q, json.loads(r["response"]))
            total += s
            db.execute("UPDATE answers SET score = ? WHERE attempt_id = ? AND question_id = ?",
                       (s, attempt_id, q["id"]))
    db.execute("UPDATE attempts SET score = ?, max_score = ? WHERE id = ?",
               (round(total, 2), round(sum(q["points"] for q in qs.values()), 2), attempt_id))


def release_time(exam: sqlite3.Row) -> datetime | None:
    """成績公布時間；None 代表交卷後立即。"""
    if exam["review_when"] == "after_close":
        return parse_time(exam["end_at"])
    if exam["review_when"] == "at_time":
        return parse_time(exam["review_at"])
    return None


def review_ready(exam: sqlite3.Row) -> bool:
    release = release_time(exam)
    return release is None or now() >= release


def review_open(exam: sqlite3.Row) -> bool:
    """事後能不能回頭查看每題作答（交卷當下那一次不受限）。"""
    if not exam["allow_review"]:
        return False
    until = parse_time(exam["review_until"])
    return until is None or now() <= until


def exam_window(exam: sqlite3.Row) -> str:
    start, end = parse_time(exam["start_at"]), parse_time(exam["end_at"])
    t = now()
    if start and t < start:
        return "not_started"
    if end and t > end:
        return "closed"
    return "open"


# ── 學生端 ──


class LoginIn(BaseModel):
    student_id: str
    password: str = ""


class PasswordIn(BaseModel):
    old_password: str = ""
    new_password: str


def public_student(s: sqlite3.Row) -> dict:
    return {"student_id": s["student_id"], "name": s["name"], "class_name": s["class_name"],
            "has_password": bool(s["password_hash"])}


class StartIn(BaseModel):
    device: str = ""


class AnswerIn(BaseModel):
    question_id: int
    response: object = None


class EventIn(BaseModel):
    type: str                           # blur / focus / time
    reason: str = ""                    # blur：window 切換視窗、fullscreen 離開全螢幕
    seconds: dict[int, float] = {}      # time：題目 id → 這段期間停留的秒數


@app.get("/api/health")
def health():
    return {"status": "ok"}


# 同一個 IP 5 分鐘內輸入不存在的學號超過這個次數就先擋住，避免被逐一試出全班學號與姓名。
# 全校學生可能共用同一個對外 IP，所以設得寬鬆
MAX_UNKNOWN_IDS = 60


@app.post("/api/login")
def login(body: LoginIn, request: Request, db=Depends(get_db)):
    sid = body.student_id.strip()
    ip = client_ip(request)
    if login_locked(f"unknown|{ip}", MAX_UNKNOWN_IDS):
        raise HTTPException(429, "嘗試次數太多，請稍後再試")
    student = db.execute("SELECT * FROM students WHERE student_id = ?", (sid,)).fetchone()
    if not student:
        record_failure(f"unknown|{ip}")
        raise HTTPException(404, "名單裡沒有這個學號，請再確認一次")
    if student["password_hash"]:
        # 設了密碼的帳號，密碼對之前不透露姓名
        if not body.password:
            return {"need_password": True}
        key = f"pw|{ip}|{sid}"
        if login_locked(key):
            raise HTTPException(429, "密碼錯誤次數太多，請 5 分鐘後再試，或請老師重設密碼")
        if not verify_password(body.password, student["password_hash"]):
            record_failure(key)
            raise HTTPException(401, "密碼錯誤")
    return {"token": student_token(sid, student["password_hash"]), "student": public_student(student)}


@app.post("/api/me/password")
def change_password(body: PasswordIn, sid: str = Depends(current_student), db=Depends(get_db)):
    student = load_student(db, sid)
    if student["password_hash"]:
        if login_locked(f"change|{sid}"):
            raise HTTPException(429, "密碼錯誤次數太多，請 5 分鐘後再試，或請老師重設密碼")
        if not verify_password(body.old_password, student["password_hash"]):
            record_failure(f"change|{sid}")
            raise HTTPException(400, "原本的密碼不正確")
    if len(body.new_password) < 4 or len(body.new_password) > 64:
        raise HTTPException(400, "新密碼長度要 4 到 64 個字元")
    new_hash = hash_password(body.new_password)
    db.execute("UPDATE students SET password_hash = ? WHERE student_id = ?", (new_hash, sid))
    # 舊 token 隨密碼失效，回傳新的讓本人不用重新登入
    return {"ok": True, "token": student_token(sid, new_hash)}


def load_student(db, sid: str) -> sqlite3.Row:
    student = db.execute("SELECT * FROM students WHERE student_id = ?", (sid,)).fetchone()
    if not student:
        raise HTTPException(401, "名單裡已沒有這個學號，請找老師")
    return student


def visible_to(exam: sqlite3.Row, student: sqlite3.Row) -> bool:
    classes = class_list(exam["class_names"])
    return bool(exam["published"]) and (not classes or student["class_name"] in classes)


@app.get("/api/exams")
def student_exams(sid: str = Depends(current_student), db=Depends(get_db)):
    student = load_student(db, sid)
    finalize_expired(db)
    result = {"server_now": now().isoformat(), "exams": []}
    for exam in db.execute("SELECT * FROM exams ORDER BY id DESC").fetchall():
        if not visible_to(exam, student):
            continue
        attempt = db.execute("SELECT * FROM attempts WHERE exam_id = ? AND student_id = ?",
                             (exam["id"], sid)).fetchone()
        n = db.execute("SELECT COUNT(*) FROM exam_questions WHERE exam_id = ?",
                       (exam["id"],)).fetchone()[0]
        start, end = parse_time(exam["start_at"]), parse_time(exam["end_at"])
        result["exams"].append({
            "id": exam["id"], "title": exam["title"], "duration_min": exam["duration_min"],
            "question_count": n, "start_at": start.isoformat() if start else "",
            "end_at": end.isoformat() if end else "", "window": exam_window(exam),
            "review_when": exam["review_when"],
            "release_at": release_time(exam).isoformat() if release_time(exam) else "",
            "allow_review": bool(exam["allow_review"]) and exam["show_mode"] != "score",
            "review_until": parse_time(exam["review_until"]).isoformat()
            if exam["review_until"] else "",
            "focus_mode": exam["focus_mode"], "focus_limit": exam["focus_limit"],
            "fullscreen": bool(exam["fullscreen"]), "block_copy": bool(exam["block_copy"]),
            "attempt": None if not attempt else {
                "id": attempt["id"], "submitted": bool(attempt["submitted_at"])},
        })
    return result


def shuffled(items: list, seed: str) -> list:
    items = list(items)
    random.Random(seed).shuffle(items)
    return items


def student_questions(db, exam: sqlite3.Row, sid: str) -> list[sqlite3.Row]:
    """這位學生看到的題目順序（打散結果依學號固定）。"""
    questions = exam_questions(db, exam["id"])
    if exam["shuffle_q"]:
        questions = shuffled(questions, f"q:{exam['id']}:{sid}")
    return questions


def option_order(exam: sqlite3.Row, sid: str, q: sqlite3.Row) -> list[int]:
    """這位學生看到的選項順序：第 k 個位置顯示原本第 order[k] 個選項。"""
    order = list(range(len(json.loads(q["options"]))))
    if exam["shuffle_opt"] and q["type"] != "fill":
        order = shuffled(order, f"o:{exam['id']}:{sid}:{q['id']}")
    return order


def question_view(db, exam: sqlite3.Row, sid: str) -> list[dict]:
    """學生作答畫面的題目（不含答案），題目與選項依學號打散。"""
    out = []
    for q in student_questions(db, exam, sid):
        texts = json.loads(q["options"])
        options = [{"i": i, "text": texts[i]} for i in option_order(exam, sid, q)]
        item = {"id": q["id"], "type": q["type"], "stem": q["stem"], "code": q["code"],
                "points": q["points"], "options": options}
        if q["type"] == "fill":
            item["blanks"] = len(json.loads(q["answer"]))
        out.append(item)
    return out


def attempt_payload(db, exam: sqlite3.Row, attempt: sqlite3.Row) -> dict:
    sid = attempt["student_id"]
    out = question_view(db, exam, sid)
    saved = {r["question_id"]: json.loads(r["response"]) for r in db.execute(
        "SELECT question_id, response FROM answers WHERE attempt_id = ?", (attempt["id"],))}
    remaining = (datetime.fromisoformat(attempt["deadline"]) - now()).total_seconds()
    return {"attempt_id": attempt["id"], "title": exam["title"],
            "remaining_seconds": max(0, int(remaining)), "questions": out, "saved": saved,
            "guard": guard_info(db, exam, attempt)}


def switches_since_unlock(db, attempt_id: int) -> int:
    """老師解鎖後重新給一輪允許次數；tab_switches 仍保留總次數給老師看。"""
    return db.execute(
        "SELECT COUNT(*) FROM attempt_log WHERE attempt_id = ? AND kind = 'blur' AND id > "
        "COALESCE((SELECT MAX(id) FROM attempt_log WHERE attempt_id = ? AND kind = 'unlock'), 0)",
        (attempt_id, attempt_id)).fetchone()[0]


def guard_info(db, exam: sqlite3.Row, attempt: sqlite3.Row) -> dict:
    return {"mode": exam["focus_mode"], "limit": exam["focus_limit"],
            "fullscreen": bool(exam["fullscreen"]), "block_copy": bool(exam["block_copy"]),
            "switches": switches_since_unlock(db, attempt["id"]), "total": attempt["tab_switches"],
            "locked": bool(attempt["locked"])}


@app.post("/api/exams/{exam_id}/start")
def start_exam(exam_id: int, body: StartIn, request: Request,
               sid: str = Depends(current_student), db=Depends(get_db)):
    student = load_student(db, sid)
    exam = exam_or_404(db, exam_id)
    if not visible_to(exam, student):
        raise HTTPException(403, "這場考試沒有開放給你")
    finalize_expired(db, exam_id)
    attempt = db.execute("SELECT * FROM attempts WHERE exam_id = ? AND student_id = ?",
                         (exam_id, sid)).fetchone()
    if attempt and attempt["submitted_at"]:
        raise HTTPException(409, "你已經交卷了")
    ip = client_ip(request)
    if not ip_allowed(ip, exam["allowed_ips"]):
        raise HTTPException(403, f"請在考場電腦作答（目前連線 IP：{ip}）")
    if not attempt:
        window = exam_window(exam)
        if window == "not_started":
            raise HTTPException(403, "考試還沒開始")
        if window == "closed":
            raise HTTPException(403, "考試已經結束")
        started = now()
        deadline = started + timedelta(minutes=exam["duration_min"])
        end = parse_time(exam["end_at"])
        if end and end < deadline:
            deadline = end.astimezone(timezone.utc)  # 一律存 UTC，finalize_expired 才比得準
        try:
            cur = db.execute(
                "INSERT INTO attempts (exam_id, student_id, started_at, deadline, ip, device) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (exam_id, sid, started.isoformat(), deadline.isoformat(), ip, body.device[:40]))
            log_event(db, cur.lastrowid, "start", detail=device_text(ip, body.device), at=started)
        except sqlite3.IntegrityError:
            pass  # 同一學生兩個分頁同時按開始，另一個已建好
        attempt = db.execute("SELECT * FROM attempts WHERE exam_id = ? AND student_id = ?",
                             (exam_id, sid)).fetchone()
    else:
        # 重新整理、換電腦接續作答都會到這裡；換電腦時記下最後使用的電腦與 IP，後台看得到
        if body.device and body.device != attempt["device"]:
            db.execute("UPDATE attempts SET device = ?, ip = ? WHERE id = ?",
                       (body.device[:40], ip, attempt["id"]))
        log_event(db, attempt["id"], "resume", detail=device_text(ip, body.device))
    return attempt_payload(db, exam, attempt)


def device_text(ip: str, device: str) -> str:
    return f"IP {ip}" + (f"，電腦 {device[:40]}" if device else "")


def own_attempt(db, attempt_id: int, sid: str) -> sqlite3.Row:
    attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt or attempt["student_id"] != sid:
        raise HTTPException(404, "找不到這份作答")
    return attempt


def valid_response(q: sqlite3.Row, response) -> bool:
    if response is None:
        return True
    n_opts = len(json.loads(q["options"]))
    if q["type"] == "single":
        return isinstance(response, int) and 0 <= response < n_opts
    if q["type"] == "multi":
        return isinstance(response, list) and len(response) <= n_opts and all(
            isinstance(i, int) and 0 <= i < n_opts for i in response)
    blanks = len(json.loads(q["answer"]))
    return isinstance(response, list) and len(response) <= blanks and all(
        isinstance(s, str) and len(s) <= 200 for s in response)


def require_exam_ip(db, attempt: sqlite3.Row, request: Request) -> None:
    # 不只開始作答時檢查：否則學生在考場進場後，把 token 交給校外的人就能代答
    exam = exam_or_404(db, attempt["exam_id"])
    ip = client_ip(request)
    if not ip_allowed(ip, exam["allowed_ips"]):
        raise HTTPException(403, f"請在考場電腦作答（目前連線 IP：{ip}）")


@app.put("/api/attempts/{attempt_id}/answers")
def save_answer(attempt_id: int, body: AnswerIn, request: Request,
                sid: str = Depends(current_student), db=Depends(get_db)):
    attempt = own_attempt(db, attempt_id, sid)
    if attempt["submitted_at"]:
        raise HTTPException(409, "已經交卷，無法再修改")
    if attempt["locked"]:
        raise HTTPException(423, "離開考試畫面次數過多，作答已鎖定，請舉手找老師解鎖")
    require_exam_ip(db, attempt, request)
    if now() > datetime.fromisoformat(attempt["deadline"]) + timedelta(seconds=GRACE_SECONDS):
        finalize(db, attempt_id, "auto_submit")
        raise HTTPException(409, "作答時間已到，系統已自動交卷")
    q = db.execute(
        "SELECT q.* FROM exam_questions eq JOIN questions q ON q.id = eq.question_id "
        "WHERE eq.exam_id = ? AND q.id = ?", (attempt["exam_id"], body.question_id)).fetchone()
    if not q or not valid_response(q, body.response):
        raise HTTPException(400, "作答格式不正確")
    if isinstance(body.response, list) and q["type"] == "multi":
        body.response = sorted(set(body.response))
    # 拿寫入鎖後再確認一次沒交卷，避免和伺服器代交同時發生時答案存進已交的卷
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT submitted_at FROM attempts WHERE id = ?",
                      (attempt_id,)).fetchone()["submitted_at"]:
            db.execute("ROLLBACK")
            raise HTTPException(409, "已經交卷，無法再修改")
        text = json.dumps(body.response, ensure_ascii=False)
        t = now()
        db.execute(
            "INSERT INTO answers (attempt_id, question_id, response, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (attempt_id, question_id) DO UPDATE SET "
            "response = excluded.response, updated_at = excluded.updated_at",
            (attempt_id, q["id"], text, t.isoformat()))
        # 填充題邊打字邊存、多選題連續勾選，15 秒內同一題併成一筆，紀錄才看得出真正改了幾次
        last = db.execute("SELECT id, kind, question_id, at FROM attempt_log "
                          "WHERE attempt_id = ? ORDER BY id DESC LIMIT 1", (attempt_id,)).fetchone()
        if (q["type"] != "single" and last and last["kind"] == "answer"
                and last["question_id"] == q["id"]
                and t - datetime.fromisoformat(last["at"]) < timedelta(seconds=15)):
            db.execute("UPDATE attempt_log SET detail = ?, at = ? WHERE id = ?",
                       (text, t.isoformat(), last["id"]))
        else:
            log_event(db, attempt_id, "answer", q["id"], text, at=t)
        db.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"ok": True}


@app.post("/api/attempts/{attempt_id}/event")
def attempt_event(attempt_id: int, body: EventIn,
                  sid: str = Depends(current_student), db=Depends(get_db)):
    attempt = own_attempt(db, attempt_id, sid)
    if attempt["submitted_at"]:
        return {"ok": True, "submitted": True}
    if body.type == "blur":
        return record_blur(db, attempt, body.reason)
    elif body.type == "focus":
        log_event(db, attempt_id, "focus")
    elif body.type == "time":
        qids = {r[0] for r in db.execute("SELECT question_id FROM exam_questions WHERE exam_id = ?",
                                         (attempt["exam_id"],))}
        # 學生端每 15 秒回報一次；單題超過 120 秒視為異常（重送或竄改），不採計
        for qid, sec in body.seconds.items():
            if qid in qids and 0 < sec <= 120:
                db.execute("INSERT INTO question_time (attempt_id, question_id, seconds) VALUES (?, ?, ?) "
                           "ON CONFLICT (attempt_id, question_id) DO UPDATE SET "
                           "seconds = seconds + excluded.seconds", (attempt_id, qid, sec))
    return {"ok": True}


def record_blur(db, attempt: sqlite3.Row, reason: str) -> dict:
    """離開考試畫面：累加次數，依考試設定警告、鎖定或交卷。判斷在伺服器，改前端也繞不過次數。"""
    exam = exam_or_404(db, attempt["exam_id"])
    aid = attempt["id"]
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("UPDATE attempts SET tab_switches = tab_switches + 1 WHERE id = ?", (aid,))
        log_event(db, aid, "blur", detail="離開全螢幕" if reason == "fullscreen" else "")
        over = switches_since_unlock(db, aid) > exam["focus_limit"]
        if over and exam["focus_mode"] == "lock" and not attempt["locked"]:
            db.execute("UPDATE attempts SET locked = 1 WHERE id = ?", (aid,))
            log_event(db, aid, "lock")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    if over and exam["focus_mode"] == "submit":
        finalize(db, aid, "violation_submit")
    attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (aid,)).fetchone()
    return {"ok": True, "submitted": bool(attempt["submitted_at"]), **guard_info(db, exam, attempt)}


def result_payload(db, attempt: sqlite3.Row, just_submitted: bool = False) -> dict:
    exam = exam_or_404(db, attempt["exam_id"])
    out = {"title": exam["title"], "score": attempt["score"], "max_score": attempt["max_score"],
           "show_mode": exam["show_mode"], "started_at": attempt["started_at"],
           "submitted_at": attempt["submitted_at"], "questions": []}
    if not review_ready(exam):
        release = release_time(exam)
        out.update(score=None, max_score=None, pending=True,
                   release_at=release.isoformat() if release else "")
        return out
    if exam["show_mode"] == "score":
        return out
    if not just_submitted and not review_open(exam):
        out["review_closed"] = True
        return out
    saved = {r["question_id"]: (json.loads(r["response"]), r["score"]) for r in db.execute(
        "SELECT * FROM answers WHERE attempt_id = ?", (attempt["id"],))}
    out["questions"] = review_items(db, exam, attempt["student_id"], saved, exam["show_mode"] == "explain")
    return out


def review_items(db, exam: sqlite3.Row, sid: str, saved: dict, explain: bool) -> list[dict]:
    """交卷後檢視的每題內容；saved：題目 id → (作答, 得分)。
    用作答時同樣的題目與選項順序呈現，字母才對得上學生當時看到的。"""
    items = []
    for q in student_questions(db, exam, sid):
        a = saved.get(q["id"])
        answer = json.loads(q["answer"])
        response = a[0] if a else None
        texts = json.loads(q["options"])
        if q["type"] != "fill":
            order = option_order(exam, sid, q)
            pos = {orig: k for k, orig in enumerate(order)}
            texts = [texts[i] for i in order]
            answer = sorted(pos[i] for i in answer)
            if isinstance(response, int):
                response = pos.get(response)
            elif isinstance(response, list):
                response = sorted(pos[i] for i in response if i in pos)
        item = {"stem": q["stem"], "code": q["code"], "type": q["type"],
                "options": texts, "answer": answer, "response": response,
                "score": a[1] if a and a[1] is not None else 0,
                "points": q["points"]}
        if explain:
            item["explanation"] = q["explanation"]
        items.append(item)
    return items


@app.post("/api/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: int, request: Request,
                   sid: str = Depends(current_student), db=Depends(get_db)):
    attempt = own_attempt(db, attempt_id, sid)
    if not attempt["submitted_at"]:
        require_exam_ip(db, attempt, request)
    finalize(db, attempt_id, "submit")
    attempt = own_attempt(db, attempt_id, sid)
    # 交卷後 2 分鐘內算「交卷當下」，一定給看詳解（若老師開放且已公布）；之後依複習設定。
    # 用時間判斷而不是「這次才交」，因為時間到可能是伺服器先代交、瀏覽器隨後才送來
    fresh = now() - datetime.fromisoformat(attempt["submitted_at"]) < timedelta(minutes=2)
    return result_payload(db, attempt, just_submitted=fresh)


@app.get("/api/attempts/{attempt_id}/result")
def attempt_result(attempt_id: int, sid: str = Depends(current_student), db=Depends(get_db)):
    attempt = own_attempt(db, attempt_id, sid)
    finalize_expired(db, attempt["exam_id"])
    attempt = own_attempt(db, attempt_id, sid)
    if not attempt["submitted_at"]:
        raise HTTPException(409, "還沒交卷")
    return result_payload(db, attempt)


# ── 老師後台 ──

admin = Depends(require_admin)


class AdminLoginIn(BaseModel):
    account: str = ""      # 留空 = 用 .env 的共用密碼登入（原本的方式）
    password: str


class CsvIn(BaseModel):
    csv: str
    replace: bool = False
    class_name: str = ""   # 名單匯入：統一設定班級（選填）


class XlsxIn(BaseModel):
    data: str              # base64
    replace: bool = False
    class_name: str = ""


class StudentIn(BaseModel):
    student_id: str
    name: str
    class_name: str = ""


class QuestionIn(BaseModel):
    type: str
    stem: str
    code: str = ""
    options: list[str] = []
    answer: list
    explanation: str = ""
    points: float = 1
    multi_mode: str = "all"            # all 全對才給分、partial 學測式、weighted 依選項比重（可倒扣）
    weights: list[float] = []          # weighted：各選項比重（百分比，例如 33.33、-16.67）
    case_sensitive: bool = False       # 填充題：區分大小寫（程式碼題建議開啟）
    tags: str = ""


class ExamIn(BaseModel):
    title: str
    start_at: str = ""
    end_at: str = ""
    duration_min: int = 50
    class_names: str = ""
    shuffle_q: bool = True
    shuffle_opt: bool = True
    show_mode: str = "score"
    review_when: str = "immediate"
    review_at: str = ""
    allow_review: bool = True
    review_until: str = ""
    allowed_ips: str = ""
    published: bool = False
    # 題目 id → 這場考試的配分；沒列的題目用題庫預設。整個欄位沒帶（None）時保留原本的配分，
    # 舊版頁面或其他工具只改考試時間時，才不會把配分全部清掉
    question_points: dict[int, float] | None = None
    focus_mode: str = "record"
    focus_limit: int = 3
    fullscreen: bool = False
    block_copy: bool = False
    question_ids: list[int] = []


class AcceptFillIn(BaseModel):
    question_id: int
    blank: int
    text: str


@app.post("/api/admin/login")
def admin_login(body: AdminLoginIn, request: Request):
    key = f"admin|{client_ip(request)}"
    if login_locked(key, limit=10, window=900):
        raise HTTPException(429, "密碼錯誤次數太多，請 15 分鐘後再試")
    account = body.account.strip()
    if account:
        return admin_account_login(account, body.password, key)
    if not check_admin_password(body.password):
        record_failure(key)
        audit.note("密碼錯誤", actor="共用密碼")
        raise HTTPException(401, "密碼錯誤")
    token = make_token("a", admin_subject(), ADMIN_TTL)
    audit.note("登入成功", session=audit.session_code(token), actor="共用密碼")
    return {"token": token, "account": "", "name": "共用密碼", "must_change": False}


def admin_account_login(account: str, password: str, ip_key: str) -> dict:
    db = connect()
    try:
        row = db.execute("SELECT * FROM admins WHERE account = ?", (account,)).fetchone()
        # 帳號不存在、密碼錯誤用同一個訊息，避免被試出有哪些帳號
        if not row or not verify_password(password, row["password_hash"]):
            record_failure(ip_key)
            audit.note("帳號或密碼錯誤", actor=account)
            raise HTTPException(401, "帳號或密碼錯誤")
        if row["disabled"]:
            audit.note("帳號已停用", actor=account)
            raise HTTPException(401, "這個管理員帳號已停用")
        db.execute("UPDATE admins SET last_login = ? WHERE account = ?", (now().isoformat(), account))
        token = admin_account_token(account, row["password_hash"])
        audit.note("登入成功" + ("（尚未修改預設密碼）" if row["must_change"] else ""),
                   session=audit.session_code(token), actor=account)
        return {"token": token, "account": account, "name": row["name"], "must_change": bool(row["must_change"])}
    finally:
        db.close()


# ── 管理員帳號 ──

ADMIN_DEFAULT_PASSWORD = os.getenv("ADMIN_DEFAULT_PASSWORD", "")


class AdminIn(BaseModel):
    account: str
    name: str = ""


class AdminUpdateIn(BaseModel):
    name: str
    disabled: bool = False


class AdminPasswordIn(BaseModel):
    old_password: str
    new_password: str


def default_admin_hash() -> str:
    if not ADMIN_DEFAULT_PASSWORD:
        raise HTTPException(500, "伺服器沒有設定 ADMIN_DEFAULT_PASSWORD（.env）")
    return hash_password(ADMIN_DEFAULT_PASSWORD)


def admin_or_404(db, account: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM admins WHERE account = ?", (account,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到這個管理員帳號")
    return row


def public_admin(r: sqlite3.Row) -> dict:
    return {"account": r["account"], "name": r["name"], "must_change": bool(r["must_change"]),
            "disabled": bool(r["disabled"]), "created_at": r["created_at"], "last_login": r["last_login"]}


@app.get("/api/admin/me")
def admin_me(me=Depends(require_admin_self)):
    return me


@app.post("/api/admin/me/password")
def change_my_password(body: AdminPasswordIn, me=Depends(require_admin_self), db=Depends(get_db)):
    if me["shared"]:
        raise HTTPException(400, "共用密碼請在伺服器的 .env 修改（ADMIN_PASSWORD）")
    row = admin_or_404(db, me["account"])
    if not verify_password(body.old_password, row["password_hash"]):
        audit.note("舊密碼錯誤")
        raise HTTPException(400, "舊密碼不正確")
    new = body.new_password
    if len(new) < 8:
        raise HTTPException(400, "新密碼至少 8 個字元")
    if new == ADMIN_DEFAULT_PASSWORD or new == me["account"] or new == body.old_password:
        raise HTTPException(400, "新密碼不能是預設密碼、帳號或舊密碼")
    pw_hash = hash_password(new)
    db.execute("UPDATE admins SET password_hash = ?, must_change = 0 WHERE account = ?", (pw_hash, me["account"]))
    audit.note(f"{me['account']} {me['name']}", target=f"管理員 {me['account']}")
    return {"ok": True, "token": admin_account_token(me["account"], pw_hash)}


@app.get("/api/admin/admins", dependencies=[admin])
def list_admins(db=Depends(get_db)):
    return [public_admin(r) for r in db.execute("SELECT * FROM admins ORDER BY account")]


@app.post("/api/admin/admins", dependencies=[admin])
def create_admin(body: AdminIn, db=Depends(get_db)):
    account = body.account.strip()
    if not account or len(account) > 40 or "|" in account or ":" in account:
        raise HTTPException(400, "帳號不能空白，最多 40 字，不能有 | 或 :")
    if db.execute("SELECT 1 FROM admins WHERE account = ?", (account,)).fetchone():
        raise HTTPException(409, "已經有這個管理員帳號")
    db.execute("INSERT INTO admins (account, name, password_hash, must_change, created_at) VALUES (?, ?, ?, 1, ?)",
               (account, body.name.strip(), default_admin_hash(), now().isoformat()))
    audit.note(f"{account} {body.name.strip()}（預設密碼，第一次登入須修改）", target=f"管理員 {account}")
    return {"ok": True}


@app.put("/api/admin/admins/{account}", dependencies=[admin])
def update_admin(account: str, body: AdminUpdateIn, me=Depends(require_admin), db=Depends(get_db)):
    old = admin_or_404(db, account)
    if account == me["account"] and body.disabled:
        raise HTTPException(400, "不能停用自己的帳號")
    diff = audit.changes({"name": old["name"], "disabled": bool(old["disabled"])},
                         {"name": body.name.strip(), "disabled": body.disabled}, {"name": "姓名", "disabled": "停用"})
    db.execute("UPDATE admins SET name = ?, disabled = ? WHERE account = ?", (body.name.strip(), int(body.disabled), account))
    audit.note(f"{account}：" + ("、".join(diff) or "沒有變更"), diff, target=f"管理員 {account}")
    return {"ok": True}


@app.post("/api/admin/admins/{account}/reset-password", dependencies=[admin])
def reset_admin_password(account: str, db=Depends(get_db)):
    admin_or_404(db, account)
    db.execute("UPDATE admins SET password_hash = ?, must_change = 1 WHERE account = ?", (default_admin_hash(), account))
    audit.note(f"{account} 改回預設密碼（下次登入須修改）", target=f"管理員 {account}")
    return {"ok": True}


@app.delete("/api/admin/admins/{account}", dependencies=[admin])
def delete_admin(account: str, me=Depends(require_admin), db=Depends(get_db)):
    old = admin_or_404(db, account)
    if account == me["account"]:
        raise HTTPException(400, "不能刪除自己的帳號")
    db.execute("DELETE FROM admins WHERE account = ?", (account,))
    audit.note(f"{account} {old['name']}", target=f"管理員 {account}")
    return {"ok": True}


@app.get("/api/admin/logs", dependencies=[admin])
def admin_logs(limit: int = 100, before: int = 0, q: str = "", action: str = "", db=Depends(get_db)):
    """後台操作日誌，新的在前；q 搜尋對象、摘要、IP、工作階段代號。"""
    sql, args = "SELECT * FROM admin_log WHERE 1 = 1", []
    if before:
        sql += " AND id < ?"
        args.append(before)
    if action:
        sql += " AND action = ?"
        args.append(action)
    if q.strip():
        like = f"%{q.strip()}%"
        sql += " AND (target LIKE ? OR summary LIKE ? OR ip LIKE ? OR session LIKE ? OR detail LIKE ?)"
        args += [like] * 5
    rows = db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
    return {"rows": [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows],
            "actions": [r[0] for r in db.execute("SELECT DISTINCT action FROM admin_log ORDER BY action")]}


@app.get("/api/admin/myip", dependencies=[admin])
def admin_myip(request: Request):
    return {"ip": client_ip(request)}


def read_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


@app.get("/api/admin/students", dependencies=[admin])
def list_students(db=Depends(get_db)):
    return [public_student(r) for r in db.execute(
        "SELECT * FROM students ORDER BY class_name, student_id")]


@app.post("/api/admin/students", dependencies=[admin])
def upsert_student(body: StudentIn, db=Depends(get_db)):
    if not body.student_id.strip() or not body.name.strip():
        raise HTTPException(400, "學號與姓名必填")
    old = db.execute("SELECT name, class_name FROM students WHERE student_id = ?", (body.student_id.strip(),)).fetchone()
    after = {"name": body.name.strip(), "class_name": body.class_name.strip()}
    diff = audit.changes(dict(old) if old else {}, after, {"name": "姓名", "class_name": "班級"})
    audit.note(("修改 " if old else "新增 ") + f"{body.student_id.strip()} {after['name']}" +
               ("：" + "、".join(diff) if old and diff else ""), diff, target=f"學生 {body.student_id.strip()}")
    db.execute("INSERT INTO students (student_id, name, class_name) VALUES (?, ?, ?) "
               "ON CONFLICT (student_id) DO UPDATE SET "
               "name = excluded.name, class_name = excluded.class_name",
               (body.student_id.strip(), body.name.strip(), body.class_name.strip()))
    ensure_class(db, body.class_name)
    return {"ok": True}


def ensure_class(db, name: str) -> None:
    if name.strip():
        db.execute("INSERT OR IGNORE INTO classes (name) VALUES (?)", (name.strip(),))


def save_roster(db, rows: list[tuple[str, str, str]], replace: bool, class_name: str) -> int:
    if not rows:
        raise HTTPException(400, "檔案裡沒有讀到任何學生")
    db.execute("BEGIN")
    try:
        if replace:
            # 有指定班級時只清這一班，不會動到其他班的名單
            sql = "DELETE FROM students WHERE student_id NOT IN (SELECT student_id FROM attempts)"
            db.execute(sql + " AND class_name = ?" if class_name.strip() else sql,
                       (class_name.strip(),) if class_name.strip() else ())
        for sid, name, cls in rows:
            db.execute("INSERT INTO students (student_id, name, class_name) VALUES (?, ?, ?) "
                       "ON CONFLICT (student_id) "
                       "DO UPDATE SET name = excluded.name, class_name = excluded.class_name",
                       (sid, name, class_name.strip() or cls))
            ensure_class(db, class_name.strip() or cls)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return len(rows)


@app.post("/api/admin/students/import", dependencies=[admin])
def import_students(body: CsvIn, db=Depends(get_db)):
    try:
        rows = parse_csv(body.csv)
    except ValueError as e:
        raise HTTPException(400, f"{e}（CSV 第一列要有 學號、姓名，或 student_id、name）")
    roster_note(rows, body.replace, body.class_name)
    return {"ok": True, "count": save_roster(db, rows, body.replace, body.class_name)}


def roster_note(rows, replace: bool, class_name: str) -> None:
    audit.note(f"{len(rows)} 人" + (f"到「{class_name}」" if class_name.strip() else "") + ("，並清除舊名單" if replace else ""),
               {"學號": [r[0] for r in rows][:500]}, target=f"班級 {class_name}" if class_name.strip() else "")


@app.post("/api/admin/students/import-xlsx", dependencies=[admin])
def import_students_xlsx(body: XlsxIn, db=Depends(get_db)):
    if len(body.data) > 14_000_000:
        raise HTTPException(400, "檔案太大（上限約 10 MB）")
    try:
        rows = parse_xlsx(body.data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    roster_note(rows, body.replace, body.class_name)
    return {"ok": True, "count": save_roster(db, rows, body.replace, body.class_name)}


# ── 班級 ──


class ClassIn(BaseModel):
    name: str


class SeatMapIn(BaseModel):
    seat_map: str


class SeatFileIn(BaseModel):
    data: str              # base64
    filename: str


def class_or_404(db, name: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM classes WHERE name = ?", (name,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到這個班級")
    return row


def valid_class_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise HTTPException(400, "班級名稱不能空白")
    if len(name) > 40 or "," in name:
        raise HTTPException(400, "班級名稱最多 40 字，不能有逗號")
    return name


def exams_of_class(db, name: str) -> list[sqlite3.Row]:
    return [e for e in db.execute("SELECT id, title, class_names FROM exams")
            if name in class_list(e["class_names"])]


@app.get("/api/admin/classes", dependencies=[admin])
def list_classes(db=Depends(get_db)):
    counts = dict(db.execute("SELECT class_name, COUNT(*) FROM students GROUP BY class_name").fetchall())
    out = []
    for c in db.execute("SELECT * FROM classes ORDER BY name"):
        grid = parse_seat_map(c["seat_map"])
        out.append({"name": c["name"], "students": counts.get(c["name"], 0),
                    "seats": sum(1 for r in grid for x in r if x),
                    "exams": len(exams_of_class(db, c["name"]))})
    return {"classes": out, "unassigned": counts.get("", 0), "total": sum(counts.values())}


@app.post("/api/admin/classes", dependencies=[admin])
def create_class(body: ClassIn, db=Depends(get_db)):
    name = valid_class_name(body.name)
    if db.execute("SELECT 1 FROM classes WHERE name = ?", (name,)).fetchone():
        raise HTTPException(409, "已經有這個班級")
    db.execute("INSERT INTO classes (name) VALUES (?)", (name,))
    audit.note(f"「{name}」", target=f"班級 {name}")
    return {"ok": True, "name": name}


@app.put("/api/admin/classes/{name}", dependencies=[admin])
def rename_class(name: str, body: ClassIn, db=Depends(get_db)):
    class_or_404(db, name)
    new = valid_class_name(body.name)
    if new == name:
        return {"ok": True, "name": new}
    if db.execute("SELECT 1 FROM classes WHERE name = ?", (new,)).fetchone():
        raise HTTPException(409, "已經有這個班級")
    db.execute("BEGIN")
    try:
        db.execute("UPDATE classes SET name = ? WHERE name = ?", (new, name))
        db.execute("UPDATE students SET class_name = ? WHERE class_name = ?", (new, name))
        # 考試的「開放班級」一起改名，不然改名後學生會看不到考試
        for e in exams_of_class(db, name):
            names = [new if c == name else c for c in class_list(e["class_names"])]
            db.execute("UPDATE exams SET class_names = ? WHERE id = ?", (",".join(names), e["id"]))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    audit.note(f"「{name}」→「{new}」", {"班級": [name, new]}, target=f"班級 {new}")
    return {"ok": True, "name": new}


@app.delete("/api/admin/classes/{name}", dependencies=[admin])
def delete_class(name: str, students: str = "keep", db=Depends(get_db)):
    """students=keep：學生移到「未分班」；students=delete：連學生一起刪（有作答紀錄的保留在未分班）。"""
    class_or_404(db, name)
    if students not in ("keep", "delete"):
        raise HTTPException(400, "students 只能是 keep 或 delete")
    db.execute("BEGIN")
    try:
        deleted = 0
        if students == "delete":
            deleted = db.execute("DELETE FROM students WHERE class_name = ? AND student_id NOT IN "
                                 "(SELECT student_id FROM attempts)", (name,)).rowcount
        kept = db.execute("UPDATE students SET class_name = '' WHERE class_name = ?", (name,)).rowcount
        for e in exams_of_class(db, name):
            names = [c for c in class_list(e["class_names"]) if c != name]
            db.execute("UPDATE exams SET class_names = ? WHERE id = ?", (",".join(names), e["id"]))
        db.execute("DELETE FROM classes WHERE name = ?", (name,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    audit.note(f"「{name}」：刪除 {deleted} 位學生、{kept} 位移到未分班", target=f"班級 {name}")
    return {"ok": True, "deleted": deleted, "unassigned": kept}


def class_seat_view(db, name: str) -> dict:
    """座位表預覽：每格對應到哪位學生，標出名單上找不到的學號與沒排到座位的學生。"""
    c = class_or_404(db, name)
    grid = parse_seat_map(c["seat_map"])
    roster = {r["student_id"]: r["name"] for r in db.execute(
        "SELECT student_id, name FROM students WHERE class_name = ?", (name,))}
    seated, unknown, cells = set(), [], []
    for row in grid:
        out = []
        for label in row:
            if not label:
                out.append(None)
                continue
            code, label_name = split_label(label)
            ok = code in roster
            if ok:
                seated.add(code)
            elif SID_LIKE.match(code):
                unknown.append(label)
            out.append({"label": label, "code": code, "name": roster.get(code, label_name),
                        "known": ok})
        cells.append(out)
    return {"name": name, "seat_map": c["seat_map"], "grid": cells, "unknown": unknown,
            "unseated": [{"student_id": s, "name": n} for s, n in sorted(roster.items()) if s not in seated]}


SID_LIKE = re.compile(r"^[A-Za-z]{0,4}\d{3,12}$")


@app.get("/api/admin/classes/{name}/seats", dependencies=[admin])
def get_class_seats(name: str, db=Depends(get_db)):
    return class_seat_view(db, name)


@app.put("/api/admin/classes/{name}/seats", dependencies=[admin])
def save_class_seats(name: str, body: SeatMapIn, db=Depends(get_db)):
    return store_class_seats(db, name, parse_seat_map(body.seat_map))


def store_class_seats(db, name: str, grid: list[list[str]]) -> dict:
    old = class_or_404(db, name)["seat_map"]
    text = grid_text(grid)
    db.execute("UPDATE classes SET seat_map = ? WHERE name = ?", (text, name))
    audit.note(f"「{name}」{sum(1 for r in grid for c in r if c)} 個座位", {"座位表": [old, text]}, target=f"班級 {name}")
    return class_seat_view(db, name)


@app.post("/api/admin/classes/{name}/seats/import", dependencies=[admin])
def import_class_seats(name: str, body: SeatFileIn, db=Depends(get_db)):
    class_or_404(db, name)
    return store_class_seats(db, name, read_seat_file(body))


def grid_text(grid: list[list[str]]) -> str:
    if len(grid) > 60 or any(len(r) > 40 for r in grid):
        raise HTTPException(400, "座位表最多 60 排、每排 40 個座位")
    # 存整理後的格子，之後在文字框裡看到、改的就是系統認得的樣子
    return "\n".join(",".join(r) for r in grid)


def read_seat_file(body) -> list[list[str]]:
    try:
        return clean_sheet(sheet_rows_from_file(base64.b64decode(body.data), body.filename))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/students/{student_id}/reset-password", dependencies=[admin])
def reset_student_password(student_id: str, db=Depends(get_db)):
    audit.note(f"{student_id} {student_label(db, student_id)}", target=f"學生 {student_id}")
    db.execute("UPDATE students SET password_hash = '' WHERE student_id = ?", (student_id,))
    return {"ok": True}


@app.delete("/api/admin/students/{student_id}", dependencies=[admin])
def delete_student(student_id: str, db=Depends(get_db)):
    if db.execute("SELECT 1 FROM attempts WHERE student_id = ?", (student_id,)).fetchone():
        raise HTTPException(409, "這位學生已有作答紀錄，不能刪除")
    audit.note(f"{student_id} {student_label(db, student_id)}", target=f"學生 {student_id}")
    db.execute("DELETE FROM students WHERE student_id = ?", (student_id,))
    return {"ok": True}


def student_label(db, sid: str) -> str:
    r = db.execute("SELECT name, class_name FROM students WHERE student_id = ?", (sid,)).fetchone()
    return f"{r['name']}（{r['class_name'] or '未分班'}）" if r else ""


def question_values(q: QuestionIn) -> tuple:
    if q.type not in ("single", "multi", "fill"):
        raise HTTPException(400, "題型只能是 single、multi、fill")
    if not q.stem.strip():
        raise HTTPException(400, "題幹不能空白")
    options = [o for o in (s.strip() for s in q.options) if o]
    if q.type == "fill":
        answer = [[str(a).strip() for a in blank if str(a).strip()] for blank in q.answer]
        if not answer or not all(answer):
            raise HTTPException(400, "填充題每一格都要有答案")
        options = []
    else:
        if len(options) < 2:
            raise HTTPException(400, "選擇題至少要兩個選項")
        answer = sorted({int(a) for a in q.answer})
        if not answer or any(a < 0 or a >= len(options) for a in answer):
            raise HTTPException(400, "正確答案不在選項範圍內")
        if q.type == "single" and len(answer) != 1:
            raise HTTPException(400, "單選題只能有一個正確答案")
    if q.multi_mode not in ("all", "partial", "weighted"):
        raise HTTPException(400, "多選計分方式只能是 all、partial 或 weighted")
    weights = ""
    if q.type == "multi" and q.multi_mode == "weighted":
        if len(q.weights) != len(options) or any(not -100 <= w <= 100 for w in q.weights):
            raise HTTPException(400, "每個選項都要設定比重（-100 到 100）")
        if abs(sum(w for w in q.weights if w > 0) - 100) > 0.1:
            raise HTTPException(400, "正確選項的比重加起來要是 100%")
        if sorted(i for i, w in enumerate(q.weights) if w > 0) != answer:
            raise HTTPException(400, "比重為正的選項要和正確答案一致")
        weights = json.dumps([round(w / 100, 6) for w in q.weights])
    # weighted 存成 partial + weights（資料表的 CHECK 限制只允許 all／partial）
    mode = "partial" if q.multi_mode == "weighted" else q.multi_mode
    return (q.type, q.stem.strip(), q.code, json.dumps(options, ensure_ascii=False),
            json.dumps(answer, ensure_ascii=False), q.explanation, q.points,
            mode, q.tags, weights, int(q.case_sensitive and q.type == "fill"))


def question_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["options"] = json.loads(d["options"])
    d["answer"] = json.loads(d["answer"])
    weights = json.loads(d.get("weights") or "[]")
    d["weights"] = [round(w * 100, 4) for w in weights]
    if weights:
        d["multi_mode"] = "weighted"
    return d


@app.get("/api/admin/questions", dependencies=[admin])
def list_questions(db=Depends(get_db)):
    return [question_dict(r) for r in db.execute("SELECT * FROM questions ORDER BY id")]


@app.post("/api/admin/questions", dependencies=[admin])
def create_question(body: QuestionIn, db=Depends(get_db)):
    cur = db.execute(
        "INSERT INTO questions (type, stem, code, options, answer, explanation, points, "
        "multi_mode, tags, weights, case_sensitive) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", question_values(body))
    audit.note(f"#{cur.lastrowid} {item_name(body.tags, body.stem)}", target=f"題目 #{cur.lastrowid}")
    return {"ok": True, "id": cur.lastrowid}


QUESTION_LABELS = {"type": "題型", "stem": "題幹", "code": "程式碼", "options": "選項", "answer": "答案",
                   "explanation": "解析", "points": "預設配分", "multi_mode": "多選計分", "tags": "標籤",
                   "weights": "選項比重", "case_sensitive": "區分大小寫"}


@app.put("/api/admin/questions/{qid}", dependencies=[admin])
def update_question(qid: int, body: QuestionIn, db=Depends(get_db)):
    values = question_values(body)
    old = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    if old:
        diff = audit.changes({k: old[k] for k in QUESTION_LABELS}, dict(zip(QUESTION_LABELS, values)), QUESTION_LABELS)
        audit.note(f"#{qid} {item_name(old['tags'], old['stem'])}" + ("：" + "、".join(diff) if diff else "（沒有變更）"),
                   diff, target=f"題目 #{qid}")
    db.execute("UPDATE questions SET type = ?, stem = ?, code = ?, options = ?, answer = ?, "
               "explanation = ?, points = ?, multi_mode = ?, tags = ?, weights = ?, case_sensitive = ? WHERE id = ?",
               (*values, qid))
    regrade_question(db, qid)
    return {"ok": True}


def regrade_question(db, qid: int) -> None:
    for r in db.execute("SELECT DISTINCT attempt_id FROM answers WHERE question_id = ?",
                        (qid,)).fetchall():
        regrade(db, r["attempt_id"])


@app.delete("/api/admin/questions/{qid}", dependencies=[admin])
def delete_question(qid: int, db=Depends(get_db)):
    if db.execute("SELECT 1 FROM exam_questions WHERE question_id = ?", (qid,)).fetchone():
        raise HTTPException(409, "這題已經被考試使用，請先從考試移除")
    old = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    if old:
        audit.note(f"#{qid} {item_name(old['tags'], old['stem'])}", {k: old[k] for k in QUESTION_LABELS}, target=f"題目 #{qid}")
    db.execute("DELETE FROM questions WHERE id = ?", (qid,))
    return {"ok": True}


LETTERS = "ABCDEF"


def parse_csv_question(r: dict) -> QuestionIn:
    qtype = r.get("type", "single").lower() or "single"
    options = [r.get(L, "") for L in LETTERS]
    while options and not options[-1]:
        options.pop()
    raw = r.get("answer", "")
    if qtype == "fill":
        answer = [[a for a in blank.split("|")] for blank in raw.split(";")]
    else:
        answer = [LETTERS.index(a.strip().upper()) for a in raw.split(";") if a.strip()]
    return QuestionIn(type=qtype, stem=r.get("stem", ""), code=r.get("code", ""),
                      options=options, answer=answer, explanation=r.get("explanation", ""),
                      points=float(r.get("points") or 1),
                      multi_mode=r.get("multi_mode") or "all", tags=r.get("tags", ""),
                      # weights 欄：各選項比重以 ; 分隔，例如 33.33;-16.67;33.33;33.33
                      weights=[float(w) for w in (r.get("weights") or "").split(";") if w.strip()],
                      case_sensitive=str(r.get("case_sensitive", "")).strip().lower() in ("1", "true", "yes", "是"))


@app.post("/api/admin/questions/import", dependencies=[admin])
def import_questions(body: CsvIn, db=Depends(get_db)):
    rows = read_csv(body.csv)
    values = []
    for n, r in enumerate(rows, start=2):
        try:
            values.append(question_values(parse_csv_question(r)))
        except HTTPException as e:
            raise HTTPException(400, f"第 {n} 列：{e.detail}")
        except (ValueError, KeyError):
            raise HTTPException(400, f"第 {n} 列：答案或分數格式不正確")
    db.execute("BEGIN")
    db.executemany(
        "INSERT INTO questions (type, stem, code, options, answer, explanation, points, "
        "multi_mode, tags, weights, case_sensitive) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    db.execute("COMMIT")
    audit.note(f"{len(values)} 題", {"題幹": [v[1][:60] for v in values][:300]})
    return {"ok": True, "count": len(values)}


def exam_values(e: ExamIn) -> tuple:
    if not e.title.strip():
        raise HTTPException(400, "考試名稱不能空白")
    if e.show_mode not in ("score", "answers", "explain"):
        raise HTTPException(400, "交卷後顯示方式不正確")
    if e.duration_min < 1:
        raise HTTPException(400, "限時至少 1 分鐘")
    if e.review_when not in ("immediate", "after_close", "at_time"):
        raise HTTPException(400, "成績公布時間不正確")
    try:
        start, end = parse_time(e.start_at), parse_time(e.end_at)
        review_at = parse_time(e.review_at)
        parse_time(e.review_until)
    except ValueError:
        raise HTTPException(400, "時間格式不正確")
    if start and end and end <= start:
        raise HTTPException(400, "關閉時間要晚於開放時間")
    if e.review_when == "after_close" and not end:
        raise HTTPException(400, "選「考試關閉後公布成績」時，要設定關閉時間")
    if e.review_when == "at_time" and not review_at:
        raise HTTPException(400, "選「指定時間公布成績」時，要設定公布時間")
    if e.focus_mode not in FOCUS_MODES:
        raise HTTPException(400, "離開考試畫面的處理方式不正確")
    if not 1 <= e.focus_limit <= 99:
        raise HTTPException(400, "允許離開次數要在 1 到 99 之間")
    return (e.title.strip(), e.start_at, e.end_at, e.duration_min, e.class_names,
            int(e.shuffle_q), int(e.shuffle_opt), e.show_mode, e.allowed_ips, int(e.published),
            e.review_when, e.review_at if e.review_when == "at_time" else "",
            int(e.allow_review), e.review_until if e.allow_review else "",
            e.focus_mode, e.focus_limit, int(e.fullscreen), int(e.block_copy))


FOCUS_MODES = ("record", "warn", "lock", "submit")


def set_exam_questions(db, exam_id: int, ids: list[int], points: dict[int, float]) -> None:
    db.execute("DELETE FROM exam_questions WHERE exam_id = ?", (exam_id,))
    seen = set()
    for pos, qid in enumerate(ids):
        if qid in seen:
            continue
        seen.add(qid)
        if not db.execute("SELECT 1 FROM questions WHERE id = ?", (qid,)).fetchone():
            raise HTTPException(400, f"題目 {qid} 不存在")
        db.execute("INSERT INTO exam_questions (exam_id, question_id, position, points) VALUES (?, ?, ?, ?)",
                   (exam_id, qid, pos, exam_point(points, qid)))


def exam_point(points: dict[int, float], qid: int) -> float | None:
    p = points.get(qid)
    if p is not None and not 0 < p <= 1000:
        raise HTTPException(400, "配分要大於 0、最多 1000")
    return p


EXAM_COLS = ("title, start_at, end_at, duration_min, class_names, shuffle_q, shuffle_opt, "
             "show_mode, allowed_ips, published, review_when, review_at, allow_review, review_until, "
             "focus_mode, focus_limit, fullscreen, block_copy")


@app.get("/api/admin/exams", dependencies=[admin])
def list_exams(db=Depends(get_db)):
    rows = db.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM exam_questions WHERE exam_id = e.id) AS question_count, "
        "(SELECT COUNT(*) FROM attempts WHERE exam_id = e.id) AS attempt_count "
        "FROM exams e ORDER BY id DESC").fetchall()
    return [{**dict(r), "window": exam_window(r)} for r in rows]


@app.get("/api/admin/exams/{exam_id}", dependencies=[admin])
def get_exam(exam_id: int, db=Depends(get_db)):
    d = dict(exam_or_404(db, exam_id))
    d["question_ids"] = [q["id"] for q in exam_questions(db, exam_id)]
    d["question_points"] = {r["question_id"]: r["points"] for r in db.execute(
        "SELECT question_id, points FROM exam_questions WHERE exam_id = ? AND points IS NOT NULL", (exam_id,))}
    return d


@app.post("/api/admin/exams", dependencies=[admin])
def create_exam(body: ExamIn, db=Depends(get_db)):
    values = exam_values(body)
    db.execute("BEGIN")
    try:
        marks = ", ".join("?" * len(values))
        cur = db.execute(f"INSERT INTO exams ({EXAM_COLS}) VALUES ({marks})", values)
        set_exam_questions(db, cur.lastrowid, body.question_ids, body.question_points or {})
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    audit.note(f"「{body.title.strip()}」", exam_snapshot(db, cur.lastrowid), target=f"考試 #{cur.lastrowid}")
    return {"ok": True, "id": cur.lastrowid}


EXAM_LABELS = {
    "title": "名稱", "start_at": "開放時間", "end_at": "關閉時間", "duration_min": "作答時間（分）",
    "class_names": "開放班級", "shuffle_q": "題目打散", "shuffle_opt": "選項打散", "show_mode": "交卷後顯示",
    "allowed_ips": "允許進場的 IP", "published": "學生看得到", "review_when": "成績公布", "review_at": "公布時間",
    "allow_review": "可回頭複習", "review_until": "複習截止", "focus_mode": "離開畫面處理",
    "focus_limit": "允許離開次數", "fullscreen": "全螢幕", "block_copy": "禁止複製", "questions": "題目（依序）",
    "points": "配分",
}


def exam_snapshot(db, exam_id: int) -> dict:
    """考試設定、題目順序與配分，給日誌比對修改前後用。"""
    e = dict(exam_or_404(db, exam_id))
    snap = {c.strip(): e[c.strip()] for c in EXAM_COLS.split(",")}
    qs = exam_questions(db, exam_id)
    snap["questions"] = [item_name(q["tags"], q["stem"]) for q in qs]
    snap["points"] = [q["points"] for q in qs]
    return snap


@app.put("/api/admin/exams/{exam_id}", dependencies=[admin])
def update_exam(exam_id: int, body: ExamIn, db=Depends(get_db)):
    exam_or_404(db, exam_id)
    values = exam_values(body)
    has_attempts = db.execute("SELECT 1 FROM attempts WHERE exam_id = ?", (exam_id,)).fetchone()
    current = [q["id"] for q in exam_questions(db, exam_id)]
    if body.question_points is None:
        body.question_points = {r["question_id"]: r["points"] for r in db.execute(
            "SELECT question_id, points FROM exam_questions WHERE exam_id = ? AND points IS NOT NULL", (exam_id,))}
    if has_attempts and body.question_ids != current:
        raise HTTPException(409, "已經有學生作答，不能再改題目；其他設定可以改")
    before = exam_snapshot(db, exam_id)
    db.execute("BEGIN")
    try:
        sets = ", ".join(f"{c.strip()} = ?" for c in EXAM_COLS.split(","))
        db.execute(f"UPDATE exams SET {sets} WHERE id = ?", (*values, exam_id))
        if not has_attempts:
            set_exam_questions(db, exam_id, body.question_ids, body.question_points)
        else:
            # 已有人作答時題目不能改，但可以調整配分，調完重新計算所有人的分數
            for qid in current:
                db.execute("UPDATE exam_questions SET points = ? WHERE exam_id = ? AND question_id = ?",
                           (exam_point(body.question_points, qid), exam_id, qid))
            for a in db.execute("SELECT id FROM attempts WHERE exam_id = ?", (exam_id,)).fetchall():
                regrade(db, a["id"])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    diff = audit.changes(before, exam_snapshot(db, exam_id), EXAM_LABELS)
    audit.note(f"「{body.title.strip()}」" + ("：" + "、".join(diff) if diff else "（沒有變更）"),
               diff, target=f"考試 #{exam_id}")
    return {"ok": True}


@app.delete("/api/admin/exams/{exam_id}", dependencies=[admin])
def delete_exam(exam_id: int, db=Depends(get_db)):
    exam = db.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if exam:
        n = db.execute("SELECT COUNT(*) FROM attempts WHERE exam_id = ?", (exam_id,)).fetchone()[0]
        audit.note(f"「{exam['title']}」，連同 {n} 份作答", exam_snapshot(db, exam_id), target=f"考試 #{exam_id}")
    db.execute("DELETE FROM exams WHERE id = ?", (exam_id,))
    return {"ok": True}


def exam_roster(db, exam: sqlite3.Row) -> list[sqlite3.Row]:
    classes = class_list(exam["class_names"])
    if classes:
        marks = ",".join("?" * len(classes))
        return db.execute(f"SELECT * FROM students WHERE class_name IN ({marks}) "
                          "ORDER BY class_name, student_id", classes).fetchall()
    return db.execute("SELECT * FROM students ORDER BY class_name, student_id").fetchall()


@app.get("/api/admin/exams/{exam_id}/monitor", dependencies=[admin])
def monitor(exam_id: int, db=Depends(get_db)):
    exam = exam_or_404(db, exam_id)
    finalize_expired(db, exam_id)
    total = db.execute("SELECT COUNT(*) FROM exam_questions WHERE exam_id = ?",
                       (exam_id,)).fetchone()[0]
    attempts = {a["student_id"]: a for a in db.execute(
        "SELECT a.*, (SELECT COUNT(*) FROM answers WHERE attempt_id = a.id) AS answered "
        "FROM attempts a WHERE exam_id = ?", (exam_id,))}
    devices: dict[str, list[str]] = {}
    for a in attempts.values():
        if a["device"]:
            devices.setdefault(a["device"], []).append(a["student_id"])
    roster = {s["student_id"]: s for s in exam_roster(db, exam)}
    # 名單外但有作答的（例如事後改了班級）也要列出
    for sid in attempts:
        if sid not in roster:
            s = db.execute("SELECT * FROM students WHERE student_id = ?", (sid,)).fetchone()
            if s:
                roster[sid] = s
    rows = []
    for sid, s in roster.items():
        a = attempts.get(sid)
        row = {"student_id": sid, "name": s["name"], "class_name": s["class_name"],
               "status": "absent", "answered": 0, "total": total, "flags": []}
        if a:
            row.update({
                "attempt_id": a["id"], "answered": a["answered"], "score": a["score"],
                "max_score": a["max_score"], "ip": a["ip"], "device": a["device"],
                "tab_switches": a["tab_switches"], "started_at": a["started_at"],
                "locked": bool(a["locked"]) and not a["submitted_at"],
                "submitted_at": a["submitted_at"],
                "status": "submitted" if a["submitted_at"] else "in_progress"})
            row["seconds"] = used_seconds(row)
            if a["device"] and len(devices[a["device"]]) > 1:
                others = [o for o in devices[a["device"]] if o != sid]
                row["flags"].append(f"同一台電腦也登入：{'、'.join(others)}")
            if a["tab_switches"] >= 3:
                row["flags"].append(f"切換視窗 {a['tab_switches']} 次")
        rows.append(row)
    return {"exam": dict(exam), "rows": rows}


def csv_text(value) -> str:
    # 電腦代碼是學生自己送的、姓名來自匯入檔：= + - @ 開頭在 Excel 會被當公式執行
    value = "" if value is None else str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@app.get("/api/admin/exams/{exam_id}/results.csv", dependencies=[admin])
def results_csv(exam_id: int, db=Depends(get_db)):
    note_exam(db, exam_id)
    data = monitor(exam_id, db)
    qs = exam_questions(db, exam_id)
    per_q = {}
    for r in db.execute("SELECT ans.attempt_id, ans.question_id, ans.score FROM answers ans "
                        "JOIN attempts a ON a.id = ans.attempt_id WHERE a.exam_id = ?",
                        (exam_id,)):
        per_q[(r["attempt_id"], r["question_id"])] = r["score"]
    buf = io.StringIO()
    buf.write("﻿")  # 讓 Excel 正確辨識 UTF-8
    w = csv.writer(buf)
    w.writerow(["學號", "姓名", "班級", "狀態", "得分", "滿分", "IP", "電腦", "切換視窗次數",
                *[f"第{i + 1}題" for i in range(len(qs))]])
    status_text = {"absent": "缺考", "in_progress": "作答中", "submitted": "已交卷"}
    for row in data["rows"]:
        aid = row.get("attempt_id")
        w.writerow([csv_text(row["student_id"]), csv_text(row["name"]),
                    csv_text(row["class_name"]), status_text[row["status"]],
                    row.get("score", ""), row.get("max_score", ""), csv_text(row.get("ip", "")),
                    csv_text(row.get("device", "")), row.get("tab_switches", ""),
                    *[per_q.get((aid, q["id"]), "") if aid else "" for q in qs]])
    filename = f"exam-{exam_id}-results.csv"
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/admin/exams/{exam_id}/stats", dependencies=[admin])
def exam_stats(exam_id: int, db=Depends(get_db)):
    exam_or_404(db, exam_id)
    out = []
    for q in exam_questions(db, exam_id):
        rows = db.execute(
            "SELECT ans.response, ans.score FROM answers ans JOIN attempts a ON a.id = ans.attempt_id "
            "WHERE a.exam_id = ? AND a.submitted_at IS NOT NULL AND ans.question_id = ?",
            (exam_id, q["id"])).fetchall()
        submitted = db.execute("SELECT COUNT(*) FROM attempts WHERE exam_id = ? "
                               "AND submitted_at IS NOT NULL", (exam_id,)).fetchone()[0]
        full = sum(1 for r in rows if r["score"] is not None and r["score"] >= q["points"])
        item = {**question_dict(q), "submitted": submitted, "answered": len(rows),
                "full_marks": full,
                "correct_rate": round(full / submitted, 3) if submitted else None}
        if q["type"] == "fill":
            answer = json.loads(q["answer"])
            variants = [dict() for _ in answer]
            for r in rows:
                resp = json.loads(r["response"]) or []
                for i in range(len(answer)):
                    text = resp[i].strip() if i < len(resp) else ""
                    variants[i][text] = variants[i].get(text, 0) + 1
            item["fill_variants"] = [
                sorted(({"text": t, "count": c, "accepted": blank_ok(q, i, t)}
                        for t, c in v.items()), key=lambda x: -x["count"])
                for i, v in enumerate(variants)]
        else:
            counts = [0] * len(json.loads(q["options"]))
            for r in rows:
                resp = json.loads(r["response"])
                for i in (resp if isinstance(resp, list) else [resp]):
                    if isinstance(i, int) and 0 <= i < len(counts):
                        counts[i] += 1
            item["option_counts"] = counts
        out.append(item)
    return out


def quantile(sorted_vals: list[float], p: float) -> float:
    # 線性內插，與 Excel 的 PERCENTILE.INC 相同
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


@app.get("/api/admin/exams/{exam_id}/scores", dependencies=[admin])
def exam_scores(exam_id: int, db=Depends(get_db)):
    data = monitor(exam_id, db)
    qs = exam_questions(db, exam_id)
    max_score = sum(q["points"] for q in qs)
    done = sorted((r for r in data["rows"] if r["status"] == "submitted"),
                  key=lambda r: -r["score"])
    rows = []
    for r in done:
        # 同分同名次
        rank = rows[-1]["rank"] if rows and rows[-1]["score"] == r["score"] else len(rows) + 1
        rows.append({"student_id": r["student_id"], "name": r["name"], "score": r["score"],
                     "rank": rank, "submitted_at": r["submitted_at"],
                     "attempt_id": r["attempt_id"], "seconds": used_seconds(r)})
    vals = sorted(r["score"] for r in rows)
    summary = {"count": len(vals),
               "absent": sum(1 for r in data["rows"] if r["status"] == "absent"),
               "in_progress": sum(1 for r in data["rows"] if r["status"] == "in_progress")}
    if vals:
        summary.update({
            "mean": round(statistics.fmean(vals), 2),
            "median": round(statistics.median(vals), 2),
            "sd": round(statistics.pstdev(vals), 2),
            "min": vals[0], "max": vals[-1],
            "q1": round(quantile(vals, .25), 2), "q3": round(quantile(vals, .75), 2)})
    # 各題得分率：已交卷者在這題的平均得分 / 配分，沒作答算 0 分
    earned = {r["question_id"]: r["total"] for r in db.execute(
        "SELECT ans.question_id, SUM(ans.score) AS total FROM answers ans "
        "JOIN attempts a ON a.id = ans.attempt_id "
        "WHERE a.exam_id = ? AND a.submitted_at IS NOT NULL GROUP BY ans.question_id",
        (exam_id,))}
    dwell = {r["question_id"]: r["avg"] for r in db.execute(
        "SELECT qt.question_id, AVG(qt.seconds) AS avg FROM question_time qt "
        "JOIN attempts a ON a.id = qt.attempt_id "
        "WHERE a.exam_id = ? AND a.submitted_at IS NOT NULL GROUP BY qt.question_id", (exam_id,))}
    questions = [{"id": q["id"], "type": q["type"], "stem": q["stem"], "points": q["points"],
                  "rate": round((earned.get(q["id"]) or 0) / (q["points"] * len(vals)), 3)
                  if vals and q["points"] else None,
                  "avg_seconds": round(dwell[q["id"]]) if q["id"] in dwell else None}
                 for q in qs]
    return {"exam": {"id": exam_id, "title": data["exam"]["title"]}, "max_score": max_score,
            "summary": summary, "rows": rows, "questions": questions}


def used_seconds(row) -> int | None:
    """作答用時：開始到交卷；還在作答就算到現在。"""
    if not row.get("started_at"):
        return None
    end = datetime.fromisoformat(row["submitted_at"]) if row.get("submitted_at") else now()
    return max(0, int((end - datetime.fromisoformat(row["started_at"])).total_seconds()))


LETTERS = "ABCDEF"


def response_text(q: sqlite3.Row, response) -> str:
    """後台顯示用：選擇題用題庫原本的選項字母（和各題分析一致），填充題用 / 分隔各格。"""
    if response is None or response == []:
        return ""
    if q["type"] == "fill":
        return " / ".join(s.strip() or "（空白）" for s in response)
    picks = response if isinstance(response, list) else [response]
    return "、".join(LETTERS[i] if i < len(LETTERS) else str(i + 1) for i in picks)


@app.get("/api/admin/attempts/{attempt_id}/log", dependencies=[admin])
def attempt_log(attempt_id: int, db=Depends(get_db)):
    attempt = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt:
        raise HTTPException(404, "找不到這份作答")
    exam = exam_or_404(db, attempt["exam_id"])
    student = db.execute("SELECT * FROM students WHERE student_id = ?",
                         (attempt["student_id"],)).fetchone()
    # 依這位學生看到的順序編題號，和學生畫面上的「第幾題」對得上
    qs = student_questions(db, exam, attempt["student_id"])
    number = {q["id"]: n + 1 for n, q in enumerate(qs)}
    times = {r["question_id"]: r["seconds"] for r in db.execute(
        "SELECT question_id, seconds FROM question_time WHERE attempt_id = ?", (attempt_id,))}
    class_avg = {r["question_id"]: r["avg"] for r in db.execute(
        "SELECT qt.question_id, AVG(qt.seconds) AS avg FROM question_time qt "
        "JOIN attempts a ON a.id = qt.attempt_id WHERE a.exam_id = ? GROUP BY qt.question_id",
        (exam["id"],))}
    saved = {r["question_id"]: r for r in db.execute(
        "SELECT * FROM answers WHERE attempt_id = ?", (attempt_id,))}
    events = db.execute("SELECT * FROM attempt_log WHERE attempt_id = ? ORDER BY at, id",
                        (attempt_id,)).fetchall()
    by_id = {q["id"]: q for q in qs}
    saves: dict[int, int] = {}
    timeline = []
    for e in events:
        item = {"at": e["at"], "kind": e["kind"], "detail": e["detail"]}
        q = by_id.get(e["question_id"])
        if e["kind"] == "answer":
            if not q:
                continue  # 題目已從考試移除
            saves[q["id"]] = saves.get(q["id"], 0) + 1
            item.update(no=number[q["id"]], detail=response_text(q, json.loads(e["detail"])),
                        change=saves[q["id"]] > 1)
        timeline.append(item)
    questions = []
    for q in qs:
        a = saved.get(q["id"])
        answer = json.loads(q["answer"])
        questions.append({
            "no": number[q["id"]], "id": q["id"], "type": q["type"], "stem": q["stem"],
            "points": q["points"], "seconds": round(times.get(q["id"], 0)),
            "avg_seconds": round(class_avg[q["id"]]) if q["id"] in class_avg else None,
            "saves": saves.get(q["id"], 0),
            "response": response_text(q, json.loads(a["response"])) if a else "",
            "answer": " / ".join(b[0] for b in answer) if q["type"] == "fill"
            else response_text(q, answer),
            "score": a["score"] if a else None})
    return {"exam": {"id": exam["id"], "title": exam["title"],
                     "duration_min": exam["duration_min"]},
            "student": {"student_id": attempt["student_id"],
                        "name": student["name"] if student else "",
                        "class_name": student["class_name"] if student else ""},
            "attempt": {**dict(attempt), "seconds": used_seconds(dict(attempt))},
            "questions": questions, "timeline": timeline}


def submitted_answers(db, exam_id: int) -> tuple[dict, dict]:
    """已交卷學生的作答：學號 → {題目 id: (標準化答案, 是否答錯, 作答時間)}，以及作答紀錄列。"""
    qs = {q["id"]: q for q in exam_questions(db, exam_id)}
    attempts = {a["student_id"]: a for a in db.execute(
        "SELECT a.*, s.name FROM attempts a LEFT JOIN students s ON s.student_id = a.student_id "
        "WHERE a.exam_id = ? AND a.submitted_at IS NOT NULL", (exam_id,))}
    data: dict[str, dict] = {sid: {} for sid in attempts}
    by_attempt = {a["id"]: sid for sid, a in attempts.items()}
    for r in db.execute("SELECT ans.* FROM answers ans JOIN attempts a ON a.id = ans.attempt_id "
                        "WHERE a.exam_id = ? AND a.submitted_at IS NOT NULL", (exam_id,)):
        q = qs.get(r["question_id"])
        resp = normalize(q["type"], json.loads(r["response"])) if q else None
        if resp is None:
            continue
        wrong = (r["score"] or 0) < q["points"]
        data[by_attempt[r["attempt_id"]]][q["id"]] = (resp, wrong, datetime.fromisoformat(r["updated_at"]))
    return data, attempts


def seat_owner(db, exam: sqlite3.Row) -> str | None:
    """只開放給一個班級的考試，座位表就是那個班級的（同班每場考試共用）；否則存在考試本身。"""
    classes = class_list(exam["class_names"])
    if len(classes) == 1 and db.execute("SELECT 1 FROM classes WHERE name = ?", (classes[0],)).fetchone():
        return classes[0]
    return None


def store_seats(db, exam_id: int, grid: list[list[str]]) -> dict:
    exam = exam_or_404(db, exam_id)
    text = grid_text(grid)
    owner = seat_owner(db, exam)
    audit.note(f"「{exam['title']}」{sum(1 for r in grid for c in r if c)} 個座位" + (f"（存到班級「{owner}」）" if owner else ""),
               {"座位表": text}, target=f"考試 #{exam_id}")
    if owner:
        db.execute("UPDATE classes SET seat_map = ? WHERE name = ?", (text, owner))
        db.execute("UPDATE exams SET seat_map = '' WHERE id = ?", (exam_id,))
    else:
        db.execute("UPDATE exams SET seat_map = ? WHERE id = ?", (text, exam_id))
    return seat_analysis(exam_id, db)


@app.put("/api/admin/exams/{exam_id}/seats", dependencies=[admin])
def save_seats(exam_id: int, body: SeatMapIn, db=Depends(get_db)):
    return store_seats(db, exam_id, parse_seat_map(body.seat_map))


@app.post("/api/admin/exams/{exam_id}/seats/import", dependencies=[admin])
def import_seats(exam_id: int, body: SeatFileIn, db=Depends(get_db)):
    return store_seats(db, exam_id, read_seat_file(body))


@app.get("/api/admin/exams/{exam_id}/seats", dependencies=[admin])
def seat_analysis(exam_id: int, db=Depends(get_db)):
    exam = exam_or_404(db, exam_id)
    owner = seat_owner(db, exam)
    seat_map = exam["seat_map"]
    if owner and not seat_map:
        seat_map = db.execute("SELECT seat_map FROM classes WHERE name = ?", (owner,)).fetchone()[0]
    prev = None if owner else db.execute(
        "SELECT id, title, seat_map FROM exams WHERE id != ? AND seat_map != '' "
        "ORDER BY id DESC LIMIT 1", (exam_id,)).fetchone()
    out = {"seat_map": seat_map, "owner": owner, "previous": dict(prev) if prev else None,
           "grid": [], "pairs": [], "others": [], "unmatched": [], "unseated": []}
    data, attempts = submitted_answers(db, exam_id)
    out["devices"] = sorted({a["device"] for a in attempts.values() if a["device"]})
    # 座位格可以填電腦代碼（學生在 /setup 設定的）或學號
    by_device: dict[str, str] = {}
    for sid, a in sorted(attempts.items(), key=lambda x: x[1]["started_at"]):
        if a["device"]:
            by_device[a["device"].strip().lower()] = sid
    grid = parse_seat_map(seat_map)
    seat_of: dict[tuple[int, int], str] = {}
    for r, row in enumerate(grid):
        cells = []
        for c, label in enumerate(row):
            if not label:
                cells.append(None)
                continue
            # 「學號姓名」格子取出學號；不是學號就當電腦代碼找
            code, _ = split_label(label)
            sid = code if code in attempts else by_device.get(label.lower())
            cell = {"label": label, "code": code, "student_id": sid, "level": 0}
            if sid:
                seat_of[(r, c)] = sid
                a = attempts[sid]
                cell.update(name=a["name"] or "", score=a["score"], max_score=a["max_score"],
                            attempt_id=a["id"])
            else:
                out["unmatched"].append(label)
            cells.append(cell)
        out["grid"].append(cells)
    seated = set(seat_of.values())
    out["unseated"] = [{"student_id": sid, "name": a["name"] or "", "device": a["device"]}
                       for sid, a in sorted(attempts.items()) if sid not in seated]
    if len(data) < 3:
        return out  # 人數太少沒有比較基準
    an = Analyzer(data)
    seated_pairs = [(p1, p2, rel, seat_of.get(p1), seat_of.get(p2)) for p1, p2, rel in neighbor_pairs(grid)]
    seated_pairs = [x for x in seated_pairs if x[3] and x[4] and x[3] != x[4]]
    adjacent = {frozenset((a, b)) for *_, a, b in seated_pairs}
    for (p1, p2, rel, a, b) in seated_pairs:
        j = an.judge(a, b, len(seated_pairs))
        out["pairs"].append({**j, "relation": rel, "relation_text": RELATION_TEXT[rel],
                             "a": person(attempts[a]), "b": person(attempts[b]),
                             "cells": [list(p1), list(p2)]})
        for (r, c) in (p1, p2):
            out["grid"][r][c]["level"] = max(out["grid"][r][c]["level"], j["level"])
    out["pairs"].sort(key=lambda x: (-x["level"], x["chance"]))
    # 不是鄰座、但相似度一樣高的組合（可能用其他方式傳答案）
    # 沒有座位上的理由，要用全班組合數校正，門檻自然比鄰座嚴格
    ids = sorted(data)
    combos = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:] if frozenset((a, b)) not in adjacent]
    others = []
    for a, b in combos:
        j = an.judge(a, b, len(combos))
        if j["level"]:
            others.append({**j, "a": person(attempts[a]), "b": person(attempts[b])})
    out["others"] = sorted(others, key=lambda x: x["chance"])[:10]
    return out


def person(a: sqlite3.Row) -> dict:
    return {"student_id": a["student_id"], "name": a["name"] or "", "score": a["score"],
            "device": a["device"], "attempt_id": a["id"]}


@app.get("/api/admin/exams/{exam_id}/compare", dependencies=[admin])
def compare_students(exam_id: int, a: str, b: str, db=Depends(get_db)):
    """兩位學生逐題對照（依考卷原本順序），標出錯得一樣與同時作答的題目。"""
    rows = {r["student_id"]: r for r in db.execute(
        "SELECT a.*, s.name FROM attempts a LEFT JOIN students s ON s.student_id = a.student_id "
        "WHERE a.exam_id = ? AND a.student_id IN (?, ?)", (exam_id, a, b))}
    if a not in rows or b not in rows:
        raise HTTPException(404, "找不到這兩位學生的作答")
    saved = {}
    for r in db.execute("SELECT * FROM answers WHERE attempt_id IN (?, ?)",
                        (rows[a]["id"], rows[b]["id"])):
        saved[(r["attempt_id"], r["question_id"])] = r
    out = []
    for n, q in enumerate(exam_questions(db, exam_id)):
        item = {"no": n + 1, "type": q["type"], "stem": q["stem"], "points": q["points"],
                "answer": " / ".join(x[0] for x in json.loads(q["answer"])) if q["type"] == "fill"
                else response_text(q, json.loads(q["answer"]))}
        sides = []
        for sid in (a, b):
            r = saved.get((rows[sid]["id"], q["id"]))
            resp = json.loads(r["response"]) if r else None
            sides.append({"text": response_text(q, resp), "norm": normalize(q["type"], resp),
                          "score": r["score"] if r else None, "at": r["updated_at"] if r else None})
        same = sides[0]["norm"] is not None and sides[0]["norm"] == sides[1]["norm"]
        wrong = [s["score"] is None or s["score"] < q["points"] for s in sides]
        gap = None
        if same and sides[0]["at"] and sides[1]["at"]:
            gap = abs((datetime.fromisoformat(sides[0]["at"]) -
                       datetime.fromisoformat(sides[1]["at"])).total_seconds())
        item.update(a=sides[0], b=sides[1], same=same, same_wrong=same and all(wrong),
                    gap_seconds=round(gap) if gap is not None else None)
        for side in sides:
            side.pop("norm")
        out.append(item)
    return {"a": person(rows[a]), "b": person(rows[b]), "questions": out}


# ── 模擬作答（管理員用學生畫面試考，不寫入資料庫）──


class PreviewSubmitIn(BaseModel):
    seed: str
    responses: dict[int, object] = {}


def preview_sid(seed: str) -> str:
    # 打散順序依這個假學號決定；出題與評分用同一個 seed，選項字母才對得上
    if not re.fullmatch(r"[0-9a-f]{8,32}", seed):
        raise HTTPException(400, "預覽代號不正確")
    return f"preview:{seed}"


@app.get("/api/admin/exams/{exam_id}/preview", dependencies=[admin])
def preview_exam(exam_id: int, db=Depends(get_db)):
    """學生作答畫面需要的資料；每次開啟都是新的打散順序，不建立作答紀錄。"""
    exam = exam_or_404(db, exam_id)
    seed = os.urandom(8).hex()
    end = parse_time(exam["end_at"])
    return {"attempt_id": 0, "preview": True, "seed": seed, "title": exam["title"],
            "remaining_seconds": exam["duration_min"] * 60, "questions": question_view(db, exam, preview_sid(seed)),
            "saved": {}, "guard": {"mode": exam["focus_mode"], "limit": exam["focus_limit"],
                                   "fullscreen": bool(exam["fullscreen"]), "block_copy": bool(exam["block_copy"]),
                                   "switches": 0, "total": 0, "locked": False},
            "exam": {"title": exam["title"], "duration_min": exam["duration_min"],
                     "question_count": len(exam_questions(db, exam_id)), "end_at": end.isoformat() if end else "",
                     "focus_mode": exam["focus_mode"], "focus_limit": exam["focus_limit"],
                     "fullscreen": bool(exam["fullscreen"]), "block_copy": bool(exam["block_copy"])}}


@app.post("/api/admin/exams/{exam_id}/preview/submit", dependencies=[admin])
def preview_submit(exam_id: int, body: PreviewSubmitIn, db=Depends(get_db)):
    """模擬交卷：照正式規則評分並回傳交卷後的檢視（一律附正確答案與解析），不寫入資料庫。"""
    exam = exam_or_404(db, exam_id)
    sid = preview_sid(body.seed)
    saved, total, max_total = {}, 0.0, 0.0
    for q in exam_questions(db, exam_id):
        max_total += q["points"]
        resp = body.responses.get(q["id"])
        if resp is None or not valid_response(q, resp):
            continue
        if q["type"] == "multi":
            resp = sorted(set(resp))
        score = score_answer(q, resp)
        saved[q["id"]] = (resp, score)
        total += score
    show = {"score": "只有總分", "answers": "總分＋每題對錯與正確答案", "explain": "總分＋對錯、正確答案與解析"}[exam["show_mode"]]
    release = release_time(exam)
    when = "交卷後立即" if release is None else f"{release.astimezone(TZ):%m/%d %H:%M} 公布"
    audit.note(f"「{exam['title']}」得分 {round(total, 2):g} / {round(max_total, 2):g}", target=f"考試 #{exam_id}")
    return {"title": exam["title"], "score": round(total, 2), "max_score": round(max_total, 2),
            "show_mode": "explain", "started_at": "", "submitted_at": now().isoformat(), "preview": True,
            "student_view": f"學生實際看到：{show}（{when}）。預覽一律顯示正確答案與解析。",
            "questions": review_items(db, exam, sid, saved, True)}


# ── 試題分析、成績總表、匯出 ──


def submitted_scores(db, exam_id: int) -> tuple[list[sqlite3.Row], dict[int, dict[int, sqlite3.Row]]]:
    """已交卷的作答（依學號）與每份作答的各題答案列。"""
    attempts = db.execute(
        "SELECT a.*, s.name, s.class_name FROM attempts a LEFT JOIN students s ON s.student_id = a.student_id "
        "WHERE a.exam_id = ? AND a.submitted_at IS NOT NULL ORDER BY a.student_id", (exam_id,)).fetchall()
    answers: dict[int, dict[int, sqlite3.Row]] = {a["id"]: {} for a in attempts}
    for r in db.execute("SELECT ans.* FROM answers ans JOIN attempts a ON a.id = ans.attempt_id "
                        "WHERE a.exam_id = ? AND a.submitted_at IS NOT NULL", (exam_id,)):
        answers[r["attempt_id"]][r["question_id"]] = r
    return attempts, answers


@app.get("/api/admin/exams/{exam_id}/items", dependencies=[admin])
def exam_items(exam_id: int, db=Depends(get_db)):
    exam = exam_or_404(db, exam_id)
    qs = exam_questions(db, exam_id)
    attempts, answers = submitted_scores(db, exam_id)
    items = []
    for q in qs:
        n_opts = len(json.loads(q["options"]))
        items.append({"id": q["id"], "points": q["points"],
                      "guess": random_guess(q["type"], n_opts, lambda resp, q=q: score_answer(q, resp) / (q["points"] or 1))})
    result = item_analysis(items, [{qid: r["score"] for qid, r in answers[a["id"]].items()} for a in attempts])
    by_id = {q["id"]: q for q in qs}
    rows = []
    for no, row in enumerate(result["items"], start=1):
        q = by_id[row["id"]]
        rows.append({**row, "no": no, "type": q["type"], "name": item_name(q["tags"], q["stem"]),
                     "stem": q["stem"], "points": q["points"], "flags": item_flags(row)})
    totals = [a["score"] for a in attempts]
    return {"exam": {"id": exam_id, "title": exam["title"]}, "count": result["count"], "alpha": result["alpha"],
            "mean": statistics.fmean(totals) if totals else None,
            "max_score": sum(q["points"] for q in qs), "items": rows}


def note_exam(db, exam_id: int) -> None:
    exam = db.execute("SELECT title FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if exam:
        audit.note(f"「{exam['title']}」", target=f"考試 #{exam_id}")


def pct(v) -> str:
    return "" if v is None else f"{v * 100:.2f}%"


@app.get("/api/admin/exams/{exam_id}/items.csv", dependencies=[admin])
def exam_items_csv(exam_id: int, db=Depends(get_db)):
    note_exam(db, exam_id)
    data = exam_items(exam_id, db)
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["題號", "題型", "概念名稱/能力指標", "配分", "作答記錄", "答對率", "標準差", "隨機猜測分數",
                "預期權重", "有效權重", "鑑別指數", "鑑別效率", "需要檢查"])
    type_text = {"single": "單選", "multi": "多選", "fill": "填充"}
    for r in data["items"]:
        w.writerow([r["no"], type_text[r["type"]], csv_text(r["name"]), r["points"], r["count"], pct(r["facility"]),
                    pct(r["sd"]), pct(r["guess"]), pct(r["intended"]), pct(r["effective"]),
                    pct(r["discrimination"]), pct(r["efficiency"]), "；".join(r["flags"])])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="exam-{exam_id}-items.csv"'})


def answer_state(q: sqlite3.Row, a: sqlite3.Row | None) -> str:
    if not a or json.loads(a["response"]) in (None, []):
        return "blank"
    score = a["score"] or 0
    return "right" if score >= q["points"] - 1e-9 else "partial" if score > 0 else "wrong"


def response_detail(q: sqlite3.Row, response) -> str:
    """成績總表「作答內容」用：選擇題附上選項文字，例如 B. int i[];"""
    if q["type"] == "fill" or response in (None, []):
        return response_text(q, response)
    options = json.loads(q["options"])
    picks = response if isinstance(response, list) else [response]
    return "；".join(f"{LETTERS[i]}. {options[i]}" for i in picks if 0 <= i < len(options))


@app.get("/api/admin/exams/{exam_id}/gradebook", dependencies=[admin])
def exam_gradebook(exam_id: int, db=Depends(get_db)):
    """仿 Moodle 成績表：每位交卷學生、每題得分與對錯，以及作答內容。"""
    exam = exam_or_404(db, exam_id)
    qs = exam_questions(db, exam_id)
    attempts, answers = submitted_scores(db, exam_id)
    rows = []
    for a in attempts:
        cells = []
        for q in qs:
            r = answers[a["id"]].get(q["id"])
            resp = json.loads(r["response"]) if r else None
            cell = {"score": r["score"] if r else None, "state": answer_state(q, r),
                    "response": response_detail(q, resp)}
            if q["type"] == "fill":
                # 逐題檢視：每一格的寫法與對錯
                key = json.loads(q["answer"])
                got = resp if isinstance(resp, list) else []
                cell["blanks"] = [{"text": got[i] if i < len(got) else "",
                                   "ok": i < len(got) and blank_ok(q, i, got[i])}
                                  for i in range(len(key))]
            cells.append(cell)
        rows.append({"attempt_id": a["id"], "student_id": a["student_id"], "name": a["name"] or "",
                     "class_name": a["class_name"] or "", "started_at": a["started_at"],
                     "submitted_at": a["submitted_at"], "seconds": used_seconds(dict(a)),
                     "score": a["score"], "cells": cells})
    owner = seat_owner(db, exam)
    return {"exam": {"id": exam_id, "title": exam["title"]},
            "questions": [{"no": n + 1, "id": q["id"], "type": q["type"], "points": q["points"],
                           "name": item_name(q["tags"], q["stem"]), "stem": q["stem"], "code": q["code"],
                           "options": json.loads(q["options"]), "answer": json.loads(q["answer"]),
                           "case_sensitive": bool(q["case_sensitive"])} for n, q in enumerate(qs)],
            "max_score": sum(q["points"] for q in qs), "rows": rows,
            "owner": owner, "grade_order": grade_order_view(db, owner) if owner else None}


class GradeOrderIn(BaseModel):
    data: str              # base64
    filename: str


@app.post("/api/admin/classes/{name}/grade-order", dependencies=[admin])
def set_grade_order(name: str, body: GradeOrderIn, db=Depends(get_db)):
    """上傳助教的成績總表，記住學生順序；之後匯出 Excel 照這個順序，整欄貼回總表就對得上。"""
    class_or_404(db, name)
    try:
        if body.filename.lower().endswith(".csv"):
            rows = parse_csv(base64.b64decode(body.data).decode("utf-8-sig", errors="replace"))
        else:
            rows = parse_xlsx(body.data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    order = [sid for sid, _, _ in rows]
    db.execute("UPDATE classes SET grade_order = ? WHERE name = ?", (json.dumps(order), name))
    audit.note(f"「{name}」{len(order)} 人，檔案 {body.filename}", {"學號順序": order}, target=f"班級 {name}")
    return grade_order_view(db, name)


@app.delete("/api/admin/classes/{name}/grade-order", dependencies=[admin])
def clear_grade_order(name: str, db=Depends(get_db)):
    class_or_404(db, name)
    audit.note(f"「{name}」", target=f"班級 {name}")
    db.execute("UPDATE classes SET grade_order = '' WHERE name = ?", (name,))
    return grade_order_view(db, name)


@app.get("/api/admin/classes/{name}/grade-order", dependencies=[admin])
def get_grade_order(name: str, db=Depends(get_db)):
    return grade_order_view(db, name)


def grade_order_view(db, name: str) -> dict:
    c = class_or_404(db, name)
    order = json.loads(c["grade_order"] or "[]")
    roster = {r["student_id"]: r["name"] for r in db.execute(
        "SELECT student_id, name FROM students WHERE class_name = ?", (name,))}
    return {"name": name, "count": len(order),
            "missing": [sid for sid in order if sid not in roster],          # 總表上有、名單沒有
            "extra": [{"student_id": s, "name": n} for s, n in sorted(roster.items()) if s not in order]}


@app.get("/api/admin/exams/{exam_id}/grades.xlsx", dependencies=[admin])
def export_grades_xlsx(exam_id: int, db=Depends(get_db)):
    """成績匯出成助教總表的格式：A 序號、C 學號、E 姓名、F 成績，第 8 列開始；學生順序照班級的成績總表。"""
    note_exam(db, exam_id)
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    exam = exam_or_404(db, exam_id)
    qs = exam_questions(db, exam_id)
    max_score = sum(q["points"] for q in qs)
    attempts, answers = submitted_scores(db, exam_id)
    by_sid = {a["student_id"]: a for a in attempts}
    owner = seat_owner(db, exam)
    order = json.loads(db.execute("SELECT grade_order FROM classes WHERE name = ?", (owner,)).fetchone()[0] or "[]") if owner else []
    roster = {r["student_id"]: r for r in exam_roster(db, exam)}
    names = {r["student_id"]: r["name"] for r in db.execute("SELECT student_id, name FROM students")}
    main = order or sorted(roster)
    extra = sorted((set(roster) | set(by_sid)) - set(main))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "成績"
    thin = Side(style="thin", color="999999")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    head = PatternFill("solid", fgColor="E7ECF2")
    ws["B2"], ws["D2"] = "班級\nClass", owner or "、".join(class_list(exam["class_names"])) or "不限"
    ws["H2"], ws["J2"] = "考試\nExam", exam["title"]
    ws["B4"] = f"匯出時間 {datetime.now(TZ):%Y-%m-%d %H:%M}；滿分 {max_score:g}；學生順序：" + \
        ("依班級成績總表" if order else "依學號（尚未上傳成績總表）")
    ws["B5"] = "F 欄可整欄複製貼到成績總表同一列範圍；未交卷留空白。"
    headers = {1: "序號\nCode#", 3: "學號 Stud#", 5: "姓名  Name", 6: f"成績/{max_score:g}", 7: "百分制"}
    for n, q in enumerate(qs):
        headers[8 + n] = f"Q{n + 1}/{q['points']:g}\n{item_name(q['tags'], q['stem'])}"
    for col, text in headers.items():
        c = ws.cell(7, col, text)
        c.font, c.fill, c.border = Font(bold=True), head, box
        c.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
    ws.row_dimensions[7].height = 44

    def write(row: int, seq, sid: str):
        a = by_sid.get(sid)
        values = {1: seq, 3: sid, 5: names.get(sid, "")}
        if a:
            values[6] = a["score"]
            values[7] = round(a["score"] / max_score * 100, 2) if max_score else None
            for n, q in enumerate(qs):
                r = answers[a["id"]].get(q["id"])
                values[8 + n] = r["score"] if r and r["score"] is not None else 0
        for col in range(1, 8 + len(qs)):
            c = ws.cell(row, col, values.get(col))
            c.border = box
            if col == 3:
                c.number_format = "@"  # 學號維持文字，前面的 0 不會不見

    row = 8
    for seq, sid in enumerate(main, start=1):
        write(row, seq, sid)
        row += 1
    if extra:
        row += 1
        ws.cell(row, 1, "以下學生不在成績總表上（貼回總表時請另外處理）").font = Font(bold=True, color="B0301C")
        row += 1
        for sid in extra:
            write(row, "", sid)
            row += 1
    for col, width in {1: 7, 2: 2, 3: 12, 4: 2, 5: 10, 6: 10, 7: 8}.items():
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = width
    for n in range(len(qs)):
        ws.column_dimensions[openpyxl.utils.get_column_letter(8 + n)].width = 12
    ws.freeze_panes = "F8"
    buf = io.BytesIO()
    wb.save(buf)
    filename = f"exam-{exam_id}-grades.xlsx"
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/admin/questions/{qid}/accept-fill", dependencies=[admin])
def accept_fill(qid: int, body: AcceptFillIn, db=Depends(get_db)):
    q = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    if not q or q["type"] != "fill":
        raise HTTPException(404, "找不到這題填充題")
    answer = json.loads(q["answer"])
    if not 0 <= body.blank < len(answer) or not body.text.strip():
        raise HTTPException(400, "空格編號或答案不正確")
    if body.text.strip() not in answer[body.blank]:
        answer[body.blank].append(body.text.strip())
    audit.note(f"#{qid} {item_name(q['tags'], q['stem'])} 第 {body.blank + 1} 格加入「{body.text.strip()}」",
               target=f"題目 #{qid}")
    db.execute("UPDATE questions SET answer = ? WHERE id = ?",
               (json.dumps(answer, ensure_ascii=False), qid))
    regrade_question(db, qid)
    return {"ok": True, "answer": answer}


@app.post("/api/admin/attempts/{attempt_id}/unlock", dependencies=[admin])
def unlock_attempt(attempt_id: int, db=Depends(get_db)):
    if not db.execute("SELECT 1 FROM attempts WHERE id = ?", (attempt_id,)).fetchone():
        raise HTTPException(404, "找不到這份作答")
    db.execute("UPDATE attempts SET locked = 0 WHERE id = ?", (attempt_id,))
    log_event(db, attempt_id, "unlock")
    note_attempt(db, attempt_id)
    return {"ok": True}


@app.post("/api/admin/attempts/{attempt_id}/force-submit", dependencies=[admin])
def force_submit(attempt_id: int, db=Depends(get_db)):
    note_attempt(db, attempt_id)
    finalize(db, attempt_id, "force_submit")
    return {"ok": True}


@app.delete("/api/admin/attempts/{attempt_id}", dependencies=[admin])
def reset_attempt(attempt_id: int, db=Depends(get_db)):
    note_attempt(db, attempt_id)
    db.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
    return {"ok": True}


def note_attempt(db, attempt_id: int) -> None:
    a = db.execute("SELECT a.*, e.title, s.name FROM attempts a JOIN exams e ON e.id = a.exam_id "
                   "LEFT JOIN students s ON s.student_id = a.student_id WHERE a.id = ?", (attempt_id,)).fetchone()
    if a:
        audit.note(f"「{a['title']}」{a['student_id']} {a['name'] or ''}",
                   {"得分": a["score"], "交卷時間": a["submitted_at"], "離開畫面次數": a["tab_switches"]},
                   target=f"考試 #{a['exam_id']}")


# ── 靜態頁面 ──


# Cloudflare 對沒有快取設定的 CSS/JS 預設讓瀏覽器快取 4 小時，改版後老師、學生會拿到舊檔。
# 頁面裡的 CSS/JS 網址加上內容雜湊（/style.css?v=ab12cd），檔案一改網址就變；頁面本身每次都重新驗證。
ASSET_REF = re.compile(r'(src|href)="/([\w.-]+\.(?:css|js))"')


@lru_cache(maxsize=None)
def render_page(name: str) -> str:
    def versioned(m: re.Match) -> str:
        digest = hashlib.sha256((STATIC_DIR / m[2]).read_bytes()).hexdigest()[:10]
        return f'{m[1]}="/{m[2]}?v={digest}"'
    return ASSET_REF.sub(versioned, (STATIC_DIR / name).read_text(encoding="utf-8"))


def page(name: str) -> HTMLResponse:
    return HTMLResponse(render_page(name), headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def student_page():
    return page("index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return page("admin.html")


@app.get("/setup", include_in_schema=False)
def setup_page():
    return page("setup.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
