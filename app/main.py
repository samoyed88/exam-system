"""考試系統後端：學生以學號進場作答，老師後台管理名單、題庫、考試與成績。"""
import asyncio
import csv
import io
import json
import os
import random
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import (
    ADMIN_TTL, STUDENT_TTL, check_admin_password, client_ip, current_student,
    ip_allowed, make_token, require_admin,
)
from .db import connect, get_db, init_db
from .grading import fill_match, grade

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


app = FastAPI(title="考試系統", docs_url=None, redoc_url=None, lifespan=lifespan)


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
    return db.execute(
        "SELECT q.* FROM exam_questions eq JOIN questions q ON q.id = eq.question_id "
        "WHERE eq.exam_id = ? ORDER BY eq.position", (exam_id,)
    ).fetchall()


def score_answer(q: sqlite3.Row, response) -> float:
    return grade(q["type"], json.loads(q["answer"]), response, q["points"],
                 q["multi_mode"], len(json.loads(q["options"])))


def finalize(db, attempt_id: int) -> None:
    """交卷並評分；已交卷就不動，可重複呼叫。"""
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
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def finalize_expired(db, exam_id: int | None = None) -> None:
    cutoff = (now() - timedelta(seconds=GRACE_SECONDS)).isoformat()
    sql = "SELECT id FROM attempts WHERE submitted_at IS NULL AND deadline < ?"
    args: list = [cutoff]
    if exam_id is not None:
        sql += " AND exam_id = ?"
        args.append(exam_id)
    for row in db.execute(sql, args).fetchall():
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


class StartIn(BaseModel):
    device: str = ""


class AnswerIn(BaseModel):
    question_id: int
    response: object = None


class EventIn(BaseModel):
    type: str


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/login")
def login(body: LoginIn, db=Depends(get_db)):
    sid = body.student_id.strip()
    student = db.execute("SELECT * FROM students WHERE student_id = ?", (sid,)).fetchone()
    if not student:
        raise HTTPException(404, "名單裡沒有這個學號，請再確認一次")
    return {"token": make_token("s", sid, STUDENT_TTL), "student": dict(student)}


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


def attempt_payload(db, exam: sqlite3.Row, attempt: sqlite3.Row) -> dict:
    sid = attempt["student_id"]
    out = []
    for q in student_questions(db, exam, sid):
        texts = json.loads(q["options"])
        options = [{"i": i, "text": texts[i]} for i in option_order(exam, sid, q)]
        item = {"id": q["id"], "type": q["type"], "stem": q["stem"], "code": q["code"],
                "points": q["points"], "options": options}
        if q["type"] == "fill":
            item["blanks"] = len(json.loads(q["answer"]))
        out.append(item)
    saved = {r["question_id"]: json.loads(r["response"]) for r in db.execute(
        "SELECT question_id, response FROM answers WHERE attempt_id = ?", (attempt["id"],))}
    remaining = (datetime.fromisoformat(attempt["deadline"]) - now()).total_seconds()
    return {"attempt_id": attempt["id"], "title": exam["title"],
            "remaining_seconds": max(0, int(remaining)), "questions": out, "saved": saved}


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
            deadline = end
        try:
            db.execute(
                "INSERT INTO attempts (exam_id, student_id, started_at, deadline, ip, device) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (exam_id, sid, started.isoformat(), deadline.isoformat(), ip, body.device[:40]))
        except sqlite3.IntegrityError:
            pass  # 同一學生兩個分頁同時按開始，另一個已建好
        attempt = db.execute("SELECT * FROM attempts WHERE exam_id = ? AND student_id = ?",
                             (exam_id, sid)).fetchone()
    elif body.device and body.device != attempt["device"]:
        # 換電腦接續作答：記下最後使用的電腦與 IP，後台看得到
        db.execute("UPDATE attempts SET device = ?, ip = ? WHERE id = ?",
                   (body.device[:40], ip, attempt["id"]))
    return attempt_payload(db, exam, attempt)


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
        return isinstance(response, list) and all(
            isinstance(i, int) and 0 <= i < n_opts for i in response)
    return isinstance(response, list) and all(
        isinstance(s, str) and len(s) <= 200 for s in response)


@app.put("/api/attempts/{attempt_id}/answers")
def save_answer(attempt_id: int, body: AnswerIn,
                sid: str = Depends(current_student), db=Depends(get_db)):
    attempt = own_attempt(db, attempt_id, sid)
    if attempt["submitted_at"]:
        raise HTTPException(409, "已經交卷，無法再修改")
    if now() > datetime.fromisoformat(attempt["deadline"]) + timedelta(seconds=GRACE_SECONDS):
        finalize(db, attempt_id)
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
        db.execute(
            "INSERT INTO answers (attempt_id, question_id, response, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (attempt_id, question_id) DO UPDATE SET "
            "response = excluded.response, updated_at = excluded.updated_at",
            (attempt_id, q["id"], json.dumps(body.response, ensure_ascii=False),
             now().isoformat()))
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
    if body.type == "blur" and not attempt["submitted_at"]:
        db.execute("UPDATE attempts SET tab_switches = tab_switches + 1 WHERE id = ?",
                   (attempt_id,))
    return {"ok": True}


def result_payload(db, attempt: sqlite3.Row) -> dict:
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
    saved = {r["question_id"]: r for r in db.execute(
        "SELECT * FROM answers WHERE attempt_id = ?", (attempt["id"],))}
    sid = attempt["student_id"]
    # 用作答時同樣的題目與選項順序呈現，字母才對得上學生當時看到的
    for q in student_questions(db, exam, sid):
        a = saved.get(q["id"])
        answer = json.loads(q["answer"])
        response = json.loads(a["response"]) if a else None
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
                "score": a["score"] if a and a["score"] is not None else 0,
                "points": q["points"]}
        if exam["show_mode"] == "explain":
            item["explanation"] = q["explanation"]
        out["questions"].append(item)
    return out


@app.post("/api/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: int, sid: str = Depends(current_student), db=Depends(get_db)):
    own_attempt(db, attempt_id, sid)
    finalize(db, attempt_id)
    return result_payload(db, own_attempt(db, attempt_id, sid))


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
    password: str


class CsvIn(BaseModel):
    csv: str
    replace: bool = False


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
    multi_mode: str = "all"
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
    allowed_ips: str = ""
    published: bool = False
    question_ids: list[int] = []


class AcceptFillIn(BaseModel):
    question_id: int
    blank: int
    text: str


@app.post("/api/admin/login")
def admin_login(body: AdminLoginIn):
    if not check_admin_password(body.password):
        raise HTTPException(401, "密碼錯誤")
    return {"token": make_token("a", "teacher", ADMIN_TTL)}


@app.get("/api/admin/myip", dependencies=[admin])
def admin_myip(request: Request):
    return {"ip": client_ip(request)}


def read_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


@app.get("/api/admin/students", dependencies=[admin])
def list_students(db=Depends(get_db)):
    return [dict(r) for r in db.execute(
        "SELECT * FROM students ORDER BY class_name, student_id")]


@app.post("/api/admin/students", dependencies=[admin])
def upsert_student(body: StudentIn, db=Depends(get_db)):
    if not body.student_id.strip() or not body.name.strip():
        raise HTTPException(400, "學號與姓名必填")
    db.execute("INSERT INTO students VALUES (?, ?, ?) ON CONFLICT (student_id) DO UPDATE SET "
               "name = excluded.name, class_name = excluded.class_name",
               (body.student_id.strip(), body.name.strip(), body.class_name.strip()))
    return {"ok": True}


@app.post("/api/admin/students/import", dependencies=[admin])
def import_students(body: CsvIn, db=Depends(get_db)):
    rows = read_csv(body.csv)
    if rows and "student_id" not in rows[0]:
        raise HTTPException(400, "CSV 第一列要有 student_id,name,class_name 標題")
    db.execute("BEGIN")
    try:
        if body.replace:
            db.execute("DELETE FROM students WHERE student_id NOT IN "
                       "(SELECT student_id FROM attempts)")
        count = 0
        for r in rows:
            if r.get("student_id") and r.get("name"):
                db.execute("INSERT INTO students VALUES (?, ?, ?) ON CONFLICT (student_id) "
                           "DO UPDATE SET name = excluded.name, class_name = excluded.class_name",
                           (r["student_id"], r["name"], r.get("class_name", "")))
                count += 1
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"ok": True, "count": count}


@app.delete("/api/admin/students/{student_id}", dependencies=[admin])
def delete_student(student_id: str, db=Depends(get_db)):
    if db.execute("SELECT 1 FROM attempts WHERE student_id = ?", (student_id,)).fetchone():
        raise HTTPException(409, "這位學生已有作答紀錄，不能刪除")
    db.execute("DELETE FROM students WHERE student_id = ?", (student_id,))
    return {"ok": True}


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
    if q.multi_mode not in ("all", "partial"):
        raise HTTPException(400, "多選計分方式只能是 all 或 partial")
    return (q.type, q.stem.strip(), q.code, json.dumps(options, ensure_ascii=False),
            json.dumps(answer, ensure_ascii=False), q.explanation, q.points,
            q.multi_mode, q.tags)


def question_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["options"] = json.loads(d["options"])
    d["answer"] = json.loads(d["answer"])
    return d


@app.get("/api/admin/questions", dependencies=[admin])
def list_questions(db=Depends(get_db)):
    return [question_dict(r) for r in db.execute("SELECT * FROM questions ORDER BY id")]


@app.post("/api/admin/questions", dependencies=[admin])
def create_question(body: QuestionIn, db=Depends(get_db)):
    cur = db.execute(
        "INSERT INTO questions (type, stem, code, options, answer, explanation, points, "
        "multi_mode, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", question_values(body))
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/admin/questions/{qid}", dependencies=[admin])
def update_question(qid: int, body: QuestionIn, db=Depends(get_db)):
    values = question_values(body)
    db.execute("UPDATE questions SET type = ?, stem = ?, code = ?, options = ?, answer = ?, "
               "explanation = ?, points = ?, multi_mode = ?, tags = ? WHERE id = ?",
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
                      multi_mode=r.get("multi_mode") or "all", tags=r.get("tags", ""))


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
        "multi_mode, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    db.execute("COMMIT")
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
    except ValueError:
        raise HTTPException(400, "時間格式不正確")
    if start and end and end <= start:
        raise HTTPException(400, "關閉時間要晚於開放時間")
    if e.review_when == "after_close" and not end:
        raise HTTPException(400, "選「考試關閉後公布成績」時，要設定關閉時間")
    if e.review_when == "at_time" and not review_at:
        raise HTTPException(400, "選「指定時間公布成績」時，要設定公布時間")
    return (e.title.strip(), e.start_at, e.end_at, e.duration_min, e.class_names,
            int(e.shuffle_q), int(e.shuffle_opt), e.show_mode, e.allowed_ips, int(e.published),
            e.review_when, e.review_at if e.review_when == "at_time" else "")


def set_exam_questions(db, exam_id: int, ids: list[int]) -> None:
    db.execute("DELETE FROM exam_questions WHERE exam_id = ?", (exam_id,))
    seen = set()
    for pos, qid in enumerate(ids):
        if qid in seen:
            continue
        seen.add(qid)
        if not db.execute("SELECT 1 FROM questions WHERE id = ?", (qid,)).fetchone():
            raise HTTPException(400, f"題目 {qid} 不存在")
        db.execute("INSERT INTO exam_questions VALUES (?, ?, ?)", (exam_id, qid, pos))


EXAM_COLS = ("title, start_at, end_at, duration_min, class_names, shuffle_q, shuffle_opt, "
             "show_mode, allowed_ips, published, review_when, review_at")


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
    return d


@app.post("/api/admin/exams", dependencies=[admin])
def create_exam(body: ExamIn, db=Depends(get_db)):
    values = exam_values(body)
    db.execute("BEGIN")
    try:
        cur = db.execute(f"INSERT INTO exams ({EXAM_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         values)
        set_exam_questions(db, cur.lastrowid, body.question_ids)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/admin/exams/{exam_id}", dependencies=[admin])
def update_exam(exam_id: int, body: ExamIn, db=Depends(get_db)):
    exam_or_404(db, exam_id)
    values = exam_values(body)
    has_attempts = db.execute("SELECT 1 FROM attempts WHERE exam_id = ?", (exam_id,)).fetchone()
    current = [q["id"] for q in exam_questions(db, exam_id)]
    if has_attempts and body.question_ids != current:
        raise HTTPException(409, "已經有學生作答，不能再改題目；其他設定可以改")
    db.execute("BEGIN")
    try:
        sets = ", ".join(f"{c.strip()} = ?" for c in EXAM_COLS.split(","))
        db.execute(f"UPDATE exams SET {sets} WHERE id = ?", (*values, exam_id))
        if not has_attempts:
            set_exam_questions(db, exam_id, body.question_ids)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"ok": True}


@app.delete("/api/admin/exams/{exam_id}", dependencies=[admin])
def delete_exam(exam_id: int, db=Depends(get_db)):
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
                "submitted_at": a["submitted_at"],
                "status": "submitted" if a["submitted_at"] else "in_progress"})
            if a["device"] and len(devices[a["device"]]) > 1:
                others = [o for o in devices[a["device"]] if o != sid]
                row["flags"].append(f"同一台電腦也登入：{'、'.join(others)}")
            if a["tab_switches"] >= 3:
                row["flags"].append(f"切換視窗 {a['tab_switches']} 次")
        rows.append(row)
    return {"exam": dict(exam), "rows": rows}


@app.get("/api/admin/exams/{exam_id}/results.csv", dependencies=[admin])
def results_csv(exam_id: int, db=Depends(get_db)):
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
        w.writerow([row["student_id"], row["name"], row["class_name"], status_text[row["status"]],
                    row.get("score", ""), row.get("max_score", ""), row.get("ip", ""),
                    row.get("device", ""), row.get("tab_switches", ""),
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
                sorted(({"text": t, "count": c, "accepted": fill_match(t, answer[i])}
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
    db.execute("UPDATE questions SET answer = ? WHERE id = ?",
               (json.dumps(answer, ensure_ascii=False), qid))
    regrade_question(db, qid)
    return {"ok": True, "answer": answer}


@app.post("/api/admin/attempts/{attempt_id}/force-submit", dependencies=[admin])
def force_submit(attempt_id: int, db=Depends(get_db)):
    finalize(db, attempt_id)
    return {"ok": True}


@app.delete("/api/admin/attempts/{attempt_id}", dependencies=[admin])
def reset_attempt(attempt_id: int, db=Depends(get_db)):
    db.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
    return {"ok": True}


# ── 靜態頁面 ──


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/setup", include_in_schema=False)
def setup_page():
    return FileResponse(STATIC_DIR / "setup.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
