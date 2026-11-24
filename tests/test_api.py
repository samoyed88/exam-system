import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ADMIN_PASSWORD"] = "teacher"
os.environ["SECRET_KEY"] = "test-secret-0123456789"
os.environ["ADMIN_DEFAULT_PASSWORD"] = "mcujava11501"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.grading import grade  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "exam.db")
    monkeypatch.delenv("IP_HEADER", raising=False)
    from app import auth
    auth._failures.clear()
    from app.main import app
    with TestClient(app) as c:
        yield c


def admin_headers(client):
    token = client.post("/api/admin/login", json={"password": "teacher"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def student_headers(client, sid):
    r = client.post("/api/login", json={"student_id": sid})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def setup_exam(client, **exam_overrides):
    h = admin_headers(client)
    students = (ROOT / "static/samples/students.csv").read_text()
    questions = (ROOT / "static/samples/questions.csv").read_text()
    assert client.post("/api/admin/students/import", json={"csv": students}, headers=h).json()["count"] == 3
    assert client.post("/api/admin/questions/import", json={"csv": questions}, headers=h).json()["count"] == 3
    exam = {"title": "小考", "duration_min": 30, "published": True, "show_mode": "explain",
            "question_ids": [1, 2, 3], **exam_overrides}
    r = client.post("/api/admin/exams", json=exam, headers=h)
    assert r.status_code == 200, r.text
    return h, r.json()["id"]


# ── 評分規則 ──

def test_grade_single_and_multi():
    assert grade("single", [0], 0, 2) == 2
    assert grade("single", [0], 1, 2) == 0
    assert grade("multi", [0, 1], [0, 1], 4) == 4
    assert grade("multi", [0, 1], [0], 4, "all", 5) == 0
    # 5 個選項錯 1 個 → (5 - 2) / 5
    assert grade("multi", [0, 1, 3, 4], [0, 1, 4], 4, "partial", 5) == pytest.approx(2.4)
    # 錯 3 個以上 → 0 分，不倒扣
    assert grade("multi", [0, 1], [2, 3, 4], 4, "partial", 5) == 0
    assert grade("multi", [0, 1], [], 4, "partial", 5) == 0


def test_grade_fill_normalizes():
    ans = [["臺北", "台北"], ["Hello World"], ["0.5"]]
    assert grade("fill", ans, ["台北", "ｈｅｌｌｏ  world", "1/2"], 3) == 3
    assert grade("fill", ans, ["高雄", "hello world ", ""], 3) == 1
    assert grade("fill", ans, None, 3) == 0


# ── 學生流程 ──

def test_unknown_student_rejected(client):
    setup_exam(client)
    assert client.post("/api/login", json={"student_id": "99999"}).status_code == 404


def test_full_exam_flow(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    exams = client.get("/api/exams", headers=s).json()["exams"]
    assert [e["id"] for e in exams] == [exam_id]

    started = client.post(f"/api/exams/{exam_id}/start", json={"device": "A01"}, headers=s).json()
    assert len(started["questions"]) == 3
    assert all("answer" not in q for q in started["questions"])
    fill = next(q for q in started["questions"] if q["type"] == "fill")
    assert fill["blanks"] == 2

    aid = started["attempt_id"]
    save = lambda qid, resp: client.put(f"/api/attempts/{aid}/answers",
                                        json={"question_id": qid, "response": resp}, headers=s)
    assert save(1, 0).status_code == 200
    assert save(2, [0, 1, 3]).status_code == 200      # 少選 E，部分給分
    assert save(3, ["台北", "新北"]).status_code == 200
    assert save(1, 9).status_code == 400              # 選項超出範圍

    # 重新整理：再按開始拿回同一份與已存答案
    again = client.post(f"/api/exams/{exam_id}/start", json={"device": "A01"}, headers=s).json()
    assert again["attempt_id"] == aid and again["saved"]["1"] == 0

    result = client.post(f"/api/attempts/{aid}/submit", headers=s).json()
    # 單選 2 + 多選 5 選項錯 1 個 4*(3/5)=2.4 + 填充 2 = 6.4
    assert result["score"] == pytest.approx(6.4)
    assert result["max_score"] == 8
    assert result["questions"][0]["explanation"]

    assert save(1, 1).status_code == 409
    assert client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).status_code == 409


def test_class_filter_and_unpublished(client):
    h, exam_id = setup_exam(client, class_names="701")
    assert client.get("/api/exams", headers=student_headers(client, "11203")).json()["exams"] == []
    assert client.post(f"/api/exams/{exam_id}/start", json={},
                       headers=student_headers(client, "11203")).status_code == 403
    _, hidden = setup_exam(client, published=False)
    ids = [e["id"] for e in client.get("/api/exams", headers=student_headers(client, "11201")).json()["exams"]]
    assert hidden not in ids


def test_ip_allowlist(client, monkeypatch):
    h, exam_id = setup_exam(client, allowed_ips="203.0.113.0/24")
    s = student_headers(client, "11201")
    # TestClient 的來源是 testclient，不在白名單
    assert client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).status_code == 403
    # 未設定 IP_HEADER 時偽造的 header 不能繞過
    spoof = {**s, "CF-Connecting-IP": "203.0.113.5"}
    assert client.post(f"/api/exams/{exam_id}/start", json={}, headers=spoof).status_code == 403
    monkeypatch.setenv("IP_HEADER", "cf")
    r = client.post(f"/api/exams/{exam_id}/start", json={}, headers=spoof)
    assert r.status_code == 200


def test_time_up_auto_submits(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    client.put(f"/api/attempts/{aid}/answers", json={"question_id": 1, "response": 0}, headers=s)
    conn = db.connect()
    conn.execute("UPDATE attempts SET deadline = '2000-01-01T00:00:00+00:00' WHERE id = ?", (aid,))
    conn.close()
    rows = client.get(f"/api/admin/exams/{exam_id}/monitor", headers=h).json()["rows"]
    me = next(r for r in rows if r["student_id"] == "11201")
    assert me["status"] == "submitted" and me["score"] == 2


def test_monitor_flags_shared_device_and_csv(client):
    h, exam_id = setup_exam(client)
    for sid in ("11201", "11202"):
        client.post(f"/api/exams/{exam_id}/start", json={"device": "A01"},
                    headers=student_headers(client, sid))
    rows = client.get(f"/api/admin/exams/{exam_id}/monitor", headers=h).json()["rows"]
    flagged = {r["student_id"] for r in rows if r["flags"]}
    assert flagged == {"11201", "11202"}
    csv_text = client.get(f"/api/admin/exams/{exam_id}/results.csv", headers=h).text
    assert "王小明" in csv_text and "缺考" in csv_text


def test_accept_fill_regrades(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    client.put(f"/api/attempts/{aid}/answers", json={"question_id": 3, "response": ["臺北市", "新北"]},
               headers=s)
    assert client.post(f"/api/attempts/{aid}/submit", headers=s).json()["score"] == 1
    stats = client.get(f"/api/admin/exams/{exam_id}/stats", headers=h).json()
    variants = next(q for q in stats if q["type"] == "fill")["fill_variants"][0]
    assert variants[0] == {"text": "臺北市", "count": 1, "accepted": False}
    r = client.post("/api/admin/questions/3/accept-fill",
                    json={"question_id": 3, "blank": 0, "text": "臺北市"}, headers=h)
    assert r.status_code == 200
    assert client.get(f"/api/attempts/{aid}/result", headers=s).json()["score"] == 2


def test_admin_requires_login(client):
    assert client.get("/api/admin/students").status_code == 401
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401


def test_cannot_change_questions_after_attempts(client):
    h, exam_id = setup_exam(client)
    client.post(f"/api/exams/{exam_id}/start", json={}, headers=student_headers(client, "11201"))
    body = {"title": "小考", "published": True, "question_ids": [1, 2]}
    assert client.put(f"/api/admin/exams/{exam_id}", json=body, headers=h).status_code == 409
    body["question_ids"] = [1, 2, 3]
    body["title"] = "小考（改名）"
    assert client.put(f"/api/admin/exams/{exam_id}", json=body, headers=h).status_code == 200


def test_background_auto_submit_without_any_request(client):
    """學生關掉瀏覽器、老師也沒開監控，伺服器仍會用已存答案代交。"""
    from app.main import _auto_submit_once
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    client.put(f"/api/attempts/{aid}/answers", json={"question_id": 1, "response": 0}, headers=s)
    client.put(f"/api/attempts/{aid}/answers",
               json={"question_id": 3, "response": ["台北", "高雄"]}, headers=s)
    conn = db.connect()
    conn.execute("UPDATE attempts SET deadline = '2000-01-01T00:00:00+00:00' WHERE id = ?", (aid,))
    _auto_submit_once()
    row = conn.execute("SELECT submitted_at, score FROM attempts WHERE id = ?", (aid,)).fetchone()
    conn.close()
    assert row["submitted_at"] and row["score"] == 3


def test_answer_rejected_after_deadline_grace(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    conn = db.connect()
    conn.execute("UPDATE attempts SET deadline = '2000-01-01T00:00:00+00:00' WHERE id = ?", (aid,))
    conn.close()
    r = client.put(f"/api/attempts/{aid}/answers", json={"question_id": 1, "response": 0}, headers=s)
    assert r.status_code == 409


def test_exam_window_and_review_after_close(client):
    h, _ = setup_exam(client)
    future = {"title": "未開始", "published": True, "question_ids": [1],
              "start_at": "2999-01-01T08:00", "end_at": "2999-01-01T09:00"}
    fid = client.post("/api/admin/exams", json=future, headers=h).json()["id"]
    s = student_headers(client, "11201")
    assert client.post(f"/api/exams/{fid}/start", json={}, headers=s).status_code == 403
    bad = {**future, "end_at": "2999-01-01T07:00"}
    assert client.post("/api/admin/exams", json=bad, headers=h).status_code == 400
    need_end = {"title": "x", "question_ids": [1], "review_when": "after_close"}
    assert client.post("/api/admin/exams", json=need_end, headers=h).status_code == 400

    later = {"title": "結束後公布", "published": True, "question_ids": [1], "show_mode": "explain",
             "review_when": "after_close", "end_at": "2999-01-01T09:00"}
    lid = client.post("/api/admin/exams", json=later, headers=h).json()["id"]
    aid = client.post(f"/api/exams/{lid}/start", json={}, headers=s).json()["attempt_id"]
    client.put(f"/api/attempts/{aid}/answers", json={"question_id": 1, "response": 0}, headers=s)
    r = client.post(f"/api/attempts/{aid}/submit", headers=s).json()
    assert r["pending"] and r["score"] is None and r["questions"] == []
    # 老師端仍看得到分數
    rows = client.get(f"/api/admin/exams/{lid}/monitor", headers=h).json()["rows"]
    assert next(x for x in rows if x["student_id"] == "11201")["score"] == 2


def test_review_uses_same_shuffled_option_letters(client):
    h, exam_id = setup_exam(client)  # 預設打散題目與選項
    s = student_headers(client, "11201")
    started = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()
    aid = started["attempt_id"]
    single = next(q for q in started["questions"] if q["type"] == "single")
    shown = [o["i"] for o in single["options"]]          # 學生看到的順序（原始索引）
    client.put(f"/api/attempts/{aid}/answers", json={"question_id": single["id"], "response": 0},
               headers=s)                                 # 選原本的第 0 個（正解）
    result = client.post(f"/api/attempts/{aid}/submit", headers=s).json()
    assert [q["stem"] for q in result["questions"]] == [q["stem"] for q in started["questions"]]
    rq = next(q for q in result["questions"] if q["type"] == "single")
    assert rq["options"] == [o["text"] for o in single["options"]]
    assert rq["response"] == shown.index(0) and rq["answer"] == [shown.index(0)]


def test_review_at_fixed_time(client):
    h, _ = setup_exam(client)
    s = student_headers(client, "11201")
    assert client.post("/api/admin/exams", headers=h, json={
        "title": "x", "question_ids": [1], "review_when": "at_time"}).status_code == 400
    eid = client.post("/api/admin/exams", headers=h, json={
        "title": "定時公布", "published": True, "question_ids": [1], "show_mode": "answers",
        "review_when": "at_time", "review_at": "2999-01-01T12:00"}).json()["id"]
    aid = client.post(f"/api/exams/{eid}/start", json={}, headers=s).json()["attempt_id"]
    client.put(f"/api/attempts/{aid}/answers", json={"question_id": 1, "response": 0}, headers=s)
    r = client.post(f"/api/attempts/{aid}/submit", headers=s).json()
    assert r["pending"] and r["score"] is None and r["release_at"].startswith("2999-01-01T12:00")
    ex = next(e for e in client.get("/api/exams", headers=s).json()["exams"] if e["id"] == eid)
    assert ex["release_at"].startswith("2999-01-01T12:00")
    # 公布時間改到過去 → 立刻看得到
    body = {"title": "定時公布", "published": True, "question_ids": [1], "show_mode": "answers",
            "review_when": "at_time", "review_at": "2000-01-01T12:00"}
    assert client.put(f"/api/admin/exams/{eid}", json=body, headers=h).status_code == 200
    r = client.get(f"/api/attempts/{aid}/result", headers=s).json()
    assert r["score"] == 2 and len(r["questions"]) == 1


def test_review_settings(client):
    h, _ = setup_exam(client)
    s = student_headers(client, "11201")

    def make(**kw):
        body = {"title": "複習", "published": True, "question_ids": [1], "show_mode": "explain", **kw}
        eid = client.post("/api/admin/exams", json=body, headers=h).json()["id"]
        aid = client.post(f"/api/exams/{eid}/start", json={}, headers=s).json()["attempt_id"]
        client.put(f"/api/attempts/{aid}/answers", json={"question_id": 1, "response": 0}, headers=s)
        return aid, client.post(f"/api/attempts/{aid}/submit", headers=s).json()

    # 不開放複習：交卷當下看得到詳解，之後只看總分
    aid, first = make(allow_review=False)
    assert len(first["questions"]) == 1
    conn = db.connect()
    conn.execute("UPDATE attempts SET submitted_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (aid,))
    conn.close()
    later = client.get(f"/api/attempts/{aid}/result", headers=s).json()
    assert later["score"] == 2 and later["questions"] == [] and later["review_closed"]

    # 複習截止已過 → 只看總分；未過 → 看得到
    aid, _ = make(review_until="2000-01-01T00:00")
    assert client.get(f"/api/attempts/{aid}/result", headers=s).json()["questions"] == []
    aid, _ = make(review_until="2999-01-01T00:00")
    assert len(client.get(f"/api/attempts/{aid}/result", headers=s).json()["questions"]) == 1


def school_gradebook_xlsx() -> str:
    """仿學校成績登記表版型的假資料：前面有標題列，第 7 列才是表頭，學號有的存成數字。"""
    import base64
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws.append([])
    ws.append([None, "班級\nClass", None, "99999測試一甲", None, None, None, "科目\nCourse"])
    ws.append([])
    ws.append([None, "(本表僅供教師原始成績紀錄，請妥善保留)"])
    ws.append([])
    ws.append([])
    ws.append(["序號\nCode#", None, "學號 Stud#", None, "姓名  Name", "Nickname", "1", "2", "期中"])
    ws.append([1, None, "90000001", None, "測試甲", "aa", 80])
    ws.append([2, None, 90000002, None, "測試乙", None, 70])
    ws.append([3, None, None, None, None])               # 空白列略過
    ws.append([4, None, "90000003", None, "測試丙", "", None])
    wb.create_sheet("TA說明").append(["工作表", "說明"])
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def test_import_school_gradebook_xlsx(client):
    h = admin_headers(client)
    r = client.post("/api/admin/students/import-xlsx", json={"data": school_gradebook_xlsx()}, headers=h)
    assert r.status_code == 200 and r.json()["count"] == 3, r.text
    students = {s["student_id"]: s for s in client.get("/api/admin/students", headers=h).json()}
    assert set(students) == {"90000001", "90000002", "90000003"}
    assert students["90000002"]["name"] == "測試乙" and students["90000002"]["class_name"] == ""
    # 可統一指定班級
    client.post("/api/admin/students/import-xlsx",
                json={"data": school_gradebook_xlsx(), "class_name": "測試一甲"}, headers=h)
    assert client.get("/api/admin/students", headers=h).json()[0]["class_name"] == "測試一甲"
    assert client.post("/api/login", json={"student_id": "90000001"}).status_code == 200
    bad = client.post("/api/admin/students/import-xlsx", json={"data": "bm90IGFuIHhsc3g="}, headers=h)
    assert bad.status_code == 400


def test_import_csv_with_chinese_headers(client):
    h = admin_headers(client)
    csv_text = "序號,學號,姓名\n1,90000011,測試丁\n2,90000012,測試戊\n"
    assert client.post("/api/admin/students/import", json={"csv": csv_text}, headers=h).json()["count"] == 2
    assert client.post("/api/admin/students/import", json={"csv": "a,b\n1,2\n"},
                       headers=h).status_code == 400


def test_student_password_flow(client):
    from app import auth
    auth._failures.clear()
    h, _ = setup_exam(client)
    # 一開始沒有密碼：只輸入學號就能登入
    first = client.post("/api/login", json={"student_id": "11201"}).json()
    assert first["student"]["has_password"] is False and "password_hash" not in first["student"]
    s = {"Authorization": f"Bearer {first['token']}"}
    assert client.post("/api/me/password", json={"new_password": "12"}, headers=s).status_code == 400
    assert client.post("/api/me/password", json={"new_password": "abcd1234"}, headers=s).status_code == 200

    # 設了密碼：沒給密碼時只回 need_password，不透露姓名
    r = client.post("/api/login", json={"student_id": "11201"}).json()
    assert r == {"need_password": True}
    assert client.post("/api/login", json={"student_id": "11201", "password": "wrong"}).status_code == 401
    ok = client.post("/api/login", json={"student_id": "11201", "password": "abcd1234"}).json()
    assert ok["student"]["has_password"] is True

    # 改密碼要舊密碼
    s = {"Authorization": f"Bearer {ok['token']}"}
    assert client.post("/api/me/password", json={"old_password": "x", "new_password": "zzzz"},
                       headers=s).status_code == 400

    # 連錯 5 次鎖定
    for _ in range(4):
        client.post("/api/login", json={"student_id": "11201", "password": "wrong"})
    assert client.post("/api/login", json={"student_id": "11201", "password": "abcd1234"}).status_code == 429

    # 老師重設後恢復只輸入學號
    auth._failures.clear()
    roster = {x["student_id"]: x for x in client.get("/api/admin/students", headers=h).json()}
    assert roster["11201"]["has_password"] and "password_hash" not in roster["11201"]
    assert client.post("/api/admin/students/11201/reset-password", headers=h).status_code == 200
    assert "token" in client.post("/api/login", json={"student_id": "11201"}).json()
    # 重新匯入名單不會清掉別人的密碼
    s2 = student_headers(client, "11202")
    client.post("/api/me/password", json={"new_password": "pppp"}, headers=s2)
    client.post("/api/admin/students/import", headers=h,
                json={"csv": (ROOT / "static/samples/students.csv").read_text()})
    assert client.post("/api/login", json={"student_id": "11202"}).json() == {"need_password": True}


def test_score_stats(client):
    h, exam_id = setup_exam(client)
    for sid, single in (("11201", 0), ("11202", 1)):
        s = student_headers(client, sid)
        aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
        client.put(f"/api/attempts/{aid}/answers", json={"question_id": 1, "response": single}, headers=s)
        client.post(f"/api/attempts/{aid}/submit", headers=s)
    data = client.get(f"/api/admin/exams/{exam_id}/scores", headers=h).json()
    assert data["max_score"] == 8
    assert [(r["student_id"], r["score"], r["rank"]) for r in data["rows"]] == [("11201", 2, 1), ("11202", 0, 2)]
    sm = data["summary"]
    assert (sm["count"], sm["absent"], sm["mean"], sm["median"], sm["min"], sm["max"]) == (2, 1, 1, 1, 0, 2)
    assert (sm["q1"], sm["q3"]) == (0.5, 1.5)
    # 第 1 題一人對一人錯 → 50%；其他題沒人作答 → 0
    assert [q["rate"] for q in data["questions"]] == [0.5, 0, 0]


def test_score_stats_ties_and_empty(client):
    h, exam_id = setup_exam(client)
    data = client.get(f"/api/admin/exams/{exam_id}/scores", headers=h).json()
    assert data["rows"] == [] and data["summary"]["count"] == 0 and "mean" not in data["summary"]
    assert all(q["rate"] is None for q in data["questions"])
    for sid in ("11201", "11202"):
        s = student_headers(client, sid)
        aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
        client.post(f"/api/attempts/{aid}/submit", headers=s)
    data = client.get(f"/api/admin/exams/{exam_id}/scores", headers=h).json()
    assert [r["rank"] for r in data["rows"]] == [1, 1]


# ── 資安 ──

def test_fill_exponent_does_not_hang():
    import time
    t = time.time()
    # 1e999999999 若交給 Fraction 展開要算好幾小時
    assert grade("fill", [["8"]], ["1e999999999"], 1) == 0
    assert grade("fill", [["0.5"]], ["1/2"], 1) == 1
    assert grade("fill", [["0.5"]], [".5"], 1) == 1
    assert time.time() - t < 1


def test_answer_size_limits(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    save = lambda qid, resp: client.put(f"/api/attempts/{aid}/answers",
                                        json={"question_id": qid, "response": resp}, headers=s)
    assert save(3, ["a", "b"]).status_code == 200
    assert save(3, ["a"] * 3).status_code == 400          # 填充題只有 2 格
    assert save(2, [0, 0, 0, 0, 0, 0]).status_code == 400  # 多選題只有 5 個選項
    big = client.put(f"/api/attempts/{aid}/answers", headers={**s, "content-type": "application/json"},
                     content=b'{"question_id": 3, "response": ["' + b"x" * 100_000 + b'"]}')
    assert big.status_code == 413


def test_ip_checked_on_save_and_submit(client, monkeypatch):
    monkeypatch.setenv("IP_HEADER", "cf")
    h, exam_id = setup_exam(client, allowed_ips="203.0.113.0/24")
    s = student_headers(client, "11201")
    inside = {**s, "CF-Connecting-IP": "203.0.113.5"}
    outside = {**s, "CF-Connecting-IP": "8.8.8.8"}
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=inside).json()["attempt_id"]
    body = {"question_id": 1, "response": 0}
    assert client.put(f"/api/attempts/{aid}/answers", json=body, headers=outside).status_code == 403
    assert client.post(f"/api/attempts/{aid}/submit", headers=outside).status_code == 403
    assert client.put(f"/api/attempts/{aid}/answers", json=body, headers=inside).status_code == 200
    assert client.post(f"/api/attempts/{aid}/submit", headers=inside).status_code == 200


def test_end_time_deadline_auto_submits(client):
    from datetime import datetime, timedelta
    from app import main
    end = (datetime.now(main.TZ) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    h, exam_id = setup_exam(client, end_at=end)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    conn = main.connect()
    deadline = conn.execute("SELECT deadline FROM attempts WHERE id = ?", (aid,)).fetchone()[0]
    assert deadline.endswith("+00:00")
    # 舊資料可能存成 +08:00：過期後仍要被代交，不能因為跟 UTC 字串比較而晚 8 小時
    past = (datetime.now(main.TZ) - timedelta(minutes=2)).isoformat()
    conn.execute("UPDATE attempts SET deadline = ? WHERE id = ?", (past, aid))
    main.finalize_expired(conn)
    assert conn.execute("SELECT submitted_at FROM attempts WHERE id = ?", (aid,)).fetchone()[0]
    conn.close()


def test_admin_login_lockout(client):
    for _ in range(10):
        assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 429
    assert client.post("/api/admin/login", json={"password": "teacher"}).status_code == 429


def test_unknown_student_ids_rate_limited(client):
    setup_exam(client)
    from app import main
    codes = [client.post("/api/login", json={"student_id": f"9{i:04d}"}).status_code
             for i in range(main.MAX_UNKNOWN_IDS + 1)]
    assert codes[:-1] == [404] * main.MAX_UNKNOWN_IDS and codes[-1] == 429


def test_password_change_revokes_old_tokens(client):
    h, _ = setup_exam(client)
    old = student_headers(client, "11201")
    r = client.post("/api/me/password", json={"old_password": "", "new_password": "pass1234"}, headers=old)
    assert client.get("/api/exams", headers=old).status_code == 401
    new = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/exams", headers=new).status_code == 200
    # 老師重設密碼後，學生手上的 token 也失效
    client.post("/api/admin/students/11201/reset-password", headers=h)
    assert client.get("/api/exams", headers=new).status_code == 401


def test_admin_token_bound_to_password(client, monkeypatch):
    h = admin_headers(client)
    assert client.get("/api/admin/exams", headers=h).status_code == 200
    from app import auth
    monkeypatch.setattr(auth, "ADMIN_PASSWORD", "changed")
    assert client.get("/api/admin/exams", headers=h).status_code == 401


def test_malformed_tokens_are_401(client):
    import base64
    for raw in ["s:11201:abc:" + "é" * 64, "s:a:b:c:d", "garbage", ""]:
        t = base64.urlsafe_b64encode(raw.encode()).decode()
        assert client.get("/api/exams", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_security_headers_and_https_redirect(client, monkeypatch):
    r = client.get("/admin")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    assert client.get("/openapi.json").status_code == 404
    monkeypatch.setenv("IP_HEADER", "cf")
    r = client.get("/admin", headers={"cf-visitor": '{"scheme":"http"}'}, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"].startswith("https://")


def test_csv_formula_injection_escaped(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    client.post(f"/api/exams/{exam_id}/start", json={"device": '=HYPERLINK("http://e/","x")'}, headers=s)
    text = client.get(f"/api/admin/exams/{exam_id}/results.csv", headers=h).text
    assert "'=HYPERLINK" in text and ',"=HYPERLINK' not in text


# ── 作答紀錄與停留時間 ──

def test_attempt_log_records_answers_and_events(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    started = client.post(f"/api/exams/{exam_id}/start", json={"device": "A01"}, headers=s).json()
    aid = started["attempt_id"]
    by_type = {q["type"]: q for q in started["questions"]}
    single = by_type["single"]
    put = lambda q, resp: client.put(f"/api/attempts/{aid}/answers",
                                     json={"question_id": q["id"], "response": resp}, headers=s)
    put(single, 0)
    put(single, 1)                      # 單選改答：另記一筆
    fill = by_type["fill"]
    blanks = fill["blanks"]
    put(fill, ["臺"] + [""] * (blanks - 1))
    put(fill, ["臺北"] + [""] * (blanks - 1))   # 邊打字邊存：併成一筆
    client.post(f"/api/attempts/{aid}/event", json={"type": "blur"}, headers=s)
    client.post(f"/api/attempts/{aid}/event", json={"type": "focus"}, headers=s)
    client.post(f"/api/exams/{exam_id}/start", json={"device": "A01"}, headers=s)  # 重新整理
    client.post(f"/api/attempts/{aid}/submit", headers=s)

    log = client.get(f"/api/admin/attempts/{aid}/log", headers=h).json()
    kinds = [e["kind"] for e in log["timeline"]]
    assert kinds == ["start", "answer", "answer", "answer", "blur", "focus", "resume", "submit"]
    answers = [e for e in log["timeline"] if e["kind"] == "answer"]
    assert [e["change"] for e in answers] == [False, True, False]
    assert answers[2]["detail"].startswith("臺北")
    rows = {q["id"]: q for q in log["questions"]}
    assert rows[single["id"]]["saves"] == 2 and rows[fill["id"]]["saves"] == 1
    assert rows[single["id"]]["response"] == "B"
    assert log["attempt"]["tab_switches"] == 1 and log["attempt"]["seconds"] >= 0
    # 題號依學生看到的順序
    assert [q["id"] for q in log["questions"]] == [q["id"] for q in started["questions"]]


def test_question_time_reported_and_validated(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    started = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()
    aid = started["attempt_id"]
    q1, q2 = started["questions"][0]["id"], started["questions"][1]["id"]
    ev = lambda sec: client.post(f"/api/attempts/{aid}/event", json={"type": "time", "seconds": sec}, headers=s)
    assert ev({q1: 15}).status_code == 200
    ev({q1: 10, q2: 5})
    ev({q2: 9999, 999: 10, q1: -5})     # 超過上限、不在考卷上的題目、負數：都不採計
    client.post(f"/api/attempts/{aid}/submit", headers=s)
    ev({q1: 30})                        # 交卷後不再累加

    rows = {q["id"]: q for q in client.get(f"/api/admin/attempts/{aid}/log", headers=h).json()["questions"]}
    assert rows[q1]["seconds"] == 25 and rows[q2]["seconds"] == 5
    assert rows[q1]["avg_seconds"] == 25
    sc = client.get(f"/api/admin/exams/{exam_id}/scores", headers=h).json()
    assert sc["rows"][0]["attempt_id"] == aid and sc["rows"][0]["seconds"] >= 0
    assert {q["id"]: q["avg_seconds"] for q in sc["questions"]}[q1] == 25


def test_attempt_log_auto_submit_force_and_reset(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    client.post(f"/api/admin/attempts/{aid}/force-submit", headers=h)
    kinds = [e["kind"] for e in client.get(f"/api/admin/attempts/{aid}/log", headers=h).json()["timeline"]]
    assert kinds == ["start", "force_submit"]
    # 學生看不到別人的紀錄、也沒有後台權限
    assert client.get(f"/api/admin/attempts/{aid}/log", headers=s).status_code == 401
    # 重考會連同紀錄一起刪掉
    client.delete(f"/api/admin/attempts/{aid}", headers=h)
    assert client.get(f"/api/admin/attempts/{aid}/log", headers=h).status_code == 404
    from app.db import connect
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM attempt_log").fetchone()[0] == 0
    conn.close()


def test_pages_use_versioned_assets(client):
    import re
    for path in ("/", "/admin", "/setup"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
        refs = re.findall(r'(?:src|href)="(/[\w.-]+\.(?:css|js)[^"]*)"', r.text)
        assert refs and all("?v=" in ref for ref in refs)
    ref = re.search(r'href="(/style\.css\?v=\w+)"', client.get("/admin").text)[1]
    assert "immutable" in client.get(ref).headers["cache-control"]
    assert client.get("/style.css").headers["cache-control"] == "no-cache"


# ── 防止切換視窗 ──

def test_focus_lock_and_unlock(client):
    h, exam_id = setup_exam(client, focus_mode="lock", focus_limit=2, fullscreen=True, block_copy=True)
    s = student_headers(client, "11201")
    ex = client.get("/api/exams", headers=s).json()["exams"][0]
    assert ex["focus_mode"] == "lock" and ex["focus_limit"] == 2 and ex["fullscreen"] and ex["block_copy"]
    started = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()
    aid, q = started["attempt_id"], started["questions"][0]
    assert started["guard"] == {"mode": "lock", "limit": 2, "fullscreen": True, "block_copy": True,
                                "switches": 0, "total": 0, "locked": False}
    blur = lambda: client.post(f"/api/attempts/{aid}/event", json={"type": "blur", "reason": "window"}, headers=s).json()
    assert blur()["locked"] is False
    assert blur()["locked"] is False          # 第 2 次還在允許範圍
    r = blur()                                # 第 3 次鎖定
    assert r["locked"] is True and r["switches"] == 3
    save = client.put(f"/api/attempts/{aid}/answers", json={"question_id": q["id"], "response": None}, headers=s)
    assert save.status_code == 423
    mon = client.get(f"/api/admin/exams/{exam_id}/monitor", headers=h).json()
    assert [r for r in mon["rows"] if r["student_id"] == "11201"][0]["locked"] is True
    # 重新進入仍是鎖定
    assert client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["guard"]["locked"] is True
    client.post(f"/api/admin/attempts/{aid}/unlock", headers=h)
    assert client.put(f"/api/attempts/{aid}/answers", json={"question_id": q["id"], "response": None},
                      headers=s).status_code == 200
    kinds = [e["kind"] for e in client.get(f"/api/admin/attempts/{aid}/log", headers=h).json()["timeline"]]
    assert kinds.count("blur") == 3 and "lock" in kinds and "unlock" in kinds
    # 解鎖後重新給一輪允許次數，總次數保留
    r = blur()
    assert r["locked"] is False and r["switches"] == 1 and r["total"] == 4
    blur()
    assert blur()["locked"] is True


def test_focus_submit_mode(client):
    h, exam_id = setup_exam(client, focus_mode="submit", focus_limit=1)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    blur = lambda: client.post(f"/api/attempts/{aid}/event", json={"type": "blur"}, headers=s).json()
    assert blur()["submitted"] is False
    assert blur()["submitted"] is True
    log = client.get(f"/api/admin/attempts/{aid}/log", headers=h).json()
    assert log["timeline"][-1]["kind"] == "violation_submit" and log["attempt"]["submitted_at"]


def test_focus_record_mode_never_locks(client):
    h, exam_id = setup_exam(client)
    s = student_headers(client, "11201")
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    for _ in range(10):
        r = client.post(f"/api/attempts/{aid}/event", json={"type": "blur"}, headers=s).json()
    assert r["switches"] == 10 and not r["locked"] and not r["submitted"]


def test_focus_settings_validated(client):
    h, _ = setup_exam(client)
    bad = {"title": "x", "question_ids": [1], "focus_mode": "explode"}
    assert client.post("/api/admin/exams", json=bad, headers=h).status_code == 400
    bad = {"title": "x", "question_ids": [1], "focus_mode": "lock", "focus_limit": 0}
    assert client.post("/api/admin/exams", json=bad, headers=h).status_code == 400


# ── 座位表與鄰座相似度 ──

def test_seat_map_parsing_and_neighbors():
    from app.seating import neighbor_pairs, parse_seat_map
    grid = parse_seat_map("A1,A2,,A3\nB1\tB2\t\tB3\n\nC1,C2")
    assert grid == [["A1", "A2", "", "A3"], ["B1", "B2", "", "B3"], ["C1", "C2", "", ""]]
    pairs = {(grid[a[0]][a[1]], grid[b[0]][b[1]], rel) for a, b, rel in neighbor_pairs(grid)}
    assert ("A1", "A2", "lr") in pairs and ("A2", "A3", "lr") not in pairs  # 走道隔開
    assert ("A3", "B3", "fb") in pairs and ("B3", "", "fb") not in pairs
    assert len(pairs) == 8  # 左右：A1A2 B1B2 C1C2；前後：A1B1 A2B2 A3B3 B1C1 B2C2

def test_tail_probabilities():
    from app.seating import count_tail, tail_probability
    assert tail_probability([.5, .5], 0) == 1
    assert abs(tail_probability([.5, .5], 2) - .25) < 1e-9
    assert abs(tail_probability([.5, .5], 1) - .75) < 1e-9
    assert tail_probability([.1] * 10, 8) < 1e-6
    assert abs(count_tail(1, 1, 1) - (1 - 2.718281828 ** -1)) < 1e-6   # Poisson
    assert count_tail(10, 1, 3) > count_tail(10, 1, 1)                  # 負二項較保守


def test_analyzer_flags_shared_wrong_answers():
    import random
    from app.seating import Analyzer
    rng = random.Random(1)
    students = {}
    for n in range(30):
        ans = {}
        for q in range(30):
            wrong = rng.random() < .4
            ans[q] = (str(rng.randrange(1, 5)) if wrong else "0", wrong, None)
        students[f"S{n:02d}"] = ans
    # S01 照抄 S00 的錯誤答案
    students["S01"] = {q: v if v[1] or rng.random() < .3 else students["S01"][q]
                       for q, v in students["S00"].items()}
    an = Analyzer(students)
    copied = an.judge("S00", "S01", tests=50)
    assert copied["level"] == 2 and copied["eeic"] >= 8 and copied["reasons"]
    honest = [an.judge(f"S{i:02d}", f"S{i + 1:02d}", tests=50) for i in range(2, 28)]
    assert not any(h["level"] for h in honest)


def test_seat_analysis_endpoint_and_compare(client):
    h, exam_id = setup_exam(client)
    started = {}
    for sid, pc in (("11201", "A01"), ("11202", "A02"), ("11203", "")):
        s = student_headers(client, sid)
        st = client.post(f"/api/exams/{exam_id}/start", json={"device": pc}, headers=s).json()
        for q in st["questions"]:
            resp = 0 if q["type"] == "single" else ([0] if q["type"] == "multi" else ["x"] * q["blanks"])
            client.put(f"/api/attempts/{st['attempt_id']}/answers", json={"question_id": q["id"], "response": resp}, headers=s)
        client.post(f"/api/attempts/{st['attempt_id']}/submit", headers=s)
        started[sid] = st
    # 座位可以填電腦代碼或學號；認不出的格子另外列出
    r = client.put(f"/api/admin/exams/{exam_id}/seats", json={"seat_map": "a01,A02\n11203,ZZ9"}, headers=h).json()
    assert r["grid"][0][0]["student_id"] == "11201" and r["grid"][1][0]["student_id"] == "11203"
    assert r["unmatched"] == ["ZZ9"] and r["unseated"] == []
    rel = {(p["a"]["student_id"], p["b"]["student_id"]): p["relation"] for p in r["pairs"]}
    assert rel == {("11201", "11202"): "lr", ("11201", "11203"): "fb"}
    assert client.get(f"/api/admin/exams/{exam_id}/seats", headers=h).json()["seat_map"] == "a01,A02\n11203,ZZ9"
    cmp = client.get(f"/api/admin/exams/{exam_id}/compare", params={"a": "11201", "b": "11202"}, headers=h).json()
    assert len(cmp["questions"]) == 3 and all(q["same"] for q in cmp["questions"])
    assert client.get(f"/api/admin/exams/{exam_id}/compare", params={"a": "11201", "b": "nobody"},
                      headers=h).status_code == 404
    too_big = "\n".join(["A"] * 61)
    assert client.put(f"/api/admin/exams/{exam_id}/seats", json={"seat_map": too_big}, headers=h).status_code == 400
    s = student_headers(client, "11201")
    assert client.get(f"/api/admin/exams/{exam_id}/seats", headers=s).status_code == 401


# 老師實際使用的座位表（Google 試算表）：白板在上、A 欄序號、合併儲存格、保留、前後門
SCHOOL_SHEET = [
    ["", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["序號", "白板", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["1", "12400212李柔萱", "", "", "", "15130841賴郁方", "", "15131586鄭紹如", "", "14133021盧君愷", "", "15130193高惟君", "", "前門"],
    ["2", "12400371魏子馨", "", "", "", "15130700陳俊宇", "", "15130893邱晴筠", "", "13131502曾俊傑", "", "15130166蔡紫瀅", "", ""],
    ["3", "12400230陳秀青", "", "15130690鐘子淇", "", "15130150蕭裕諺", "", "", "", "", "", "15130636董語瑄", "", ""],
    ["12", "15130620 周宸瑋", "", "15130884呂秉翰", "", "15130991汪家揚", "", "15131650盧俊佑", "", "14111353徐子涵", "", "保留", "", ""],
    ["13", "12131573吳旻翰", "", "15130955陳澤鈞", "", "15130786何誠儒", "", "15130645王博玄", "", "15130212王心如", "", "保留", "", "後門"],
    ["14", "保留", "", "保留", "", "保留", "", "保留", "", "保留", "", "保留", "", ""],
]


def test_school_seat_sheet_is_cleaned():
    from app.seating import neighbor_pairs, parse_seat_map, split_label
    text = "\n".join("\t".join(r) for r in SCHOOL_SHEET)
    grid = parse_seat_map(text)
    assert len(grid) == 5 and all(len(r) == 6 for r in grid)       # 6 欄座位、5 排有人
    assert grid[0] == ["12400212李柔萱", "", "15130841賴郁方", "15131586鄭紹如", "14133021盧君愷", "15130193高惟君"]
    assert grid[3][5] == "" and grid[4][0] == "12131573吳旻翰"       # 保留不是座位
    assert split_label("15130620 周宸瑋") == ("15130620", "周宸瑋")
    assert split_label("PC01") == ("PC01", "")
    pairs = {(grid[a[0]][a[1]][:8], grid[b[0]][b[1]][:8], rel) for a, b, rel in neighbor_pairs(grid)}
    assert ("15130841", "15131586", "lr") in pairs                    # 左右
    assert ("12400212", "12400371", "fb") in pairs                    # 前後
    assert not any("12400212" in (x, y) and rel == "lr" for x, y, rel in pairs)  # 旁邊是空位


def test_seat_import_xlsx_with_merged_cells(client):
    import base64
    import io
    import openpyxl
    h, exam_id = setup_exam(client)
    for sid in ("11201", "11202", "11203"):
        s = student_headers(client, sid)
        aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
        client.post(f"/api/attempts/{aid}/submit", headers=s)
    wb = openpyxl.Workbook()
    ws = wb.active
    rows = [["序號", "白板", "", "", ""], ["1", "11201王小明", "", "11202 李小華", ""], ["2", "11203陳大文", "", "保留", ""]]
    for row in rows:
        ws.append(row)
    ws.merge_cells("B1:E1")
    for r in (2, 3):
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=5)
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post(f"/api/admin/exams/{exam_id}/seats/import",
                    json={"data": base64.b64encode(buf.getvalue()).decode(), "filename": "座位表.xlsx"}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["seat_map"] == "11201王小明,11202 李小華\n11203陳大文,"
    assert [[c and c["student_id"] for c in row] for row in d["grid"]] == [["11201", "11202"], ["11203", None]]
    assert {(p["a"]["student_id"], p["b"]["student_id"]) for p in d["pairs"]} == {("11201", "11202"), ("11201", "11203")}
    bad = client.post(f"/api/admin/exams/{exam_id}/seats/import",
                      json={"data": base64.b64encode(b"not a zip").decode(), "filename": "x.xlsx"}, headers=h)
    assert bad.status_code == 400


# ── 班級（增刪改查）──

def test_class_crud(client):
    h, exam_id = setup_exam(client)
    assert client.get("/api/admin/classes", headers=h).json()["unassigned"] == 3  # 範例名單沒有班級
    assert client.post("/api/admin/classes", json={"name": "資管一乙"}, headers=h).status_code == 200
    assert client.post("/api/admin/classes", json={"name": "資管一乙"}, headers=h).status_code == 409
    assert client.post("/api/admin/classes", json={"name": " "}, headers=h).status_code == 400
    assert client.post("/api/admin/classes", json={"name": "a,b"}, headers=h).status_code == 400
    # 學生新增、改班、改名
    client.post("/api/admin/students", json={"student_id": "S1", "name": "甲", "class_name": "資管一乙"}, headers=h)
    client.post("/api/admin/students", json={"student_id": "S2", "name": "乙", "class_name": "資管一乙"}, headers=h)
    client.post("/api/admin/students", json={"student_id": "S2", "name": "乙乙", "class_name": "資管一乙"}, headers=h)
    roster = {s["student_id"]: s for s in client.get("/api/admin/students", headers=h).json()}
    assert roster["S2"]["name"] == "乙乙"
    # 考試只開放給資管一乙
    exam = client.get(f"/api/admin/exams/{exam_id}", headers=h).json()
    exam.update(class_names="資管一乙")
    assert client.put(f"/api/admin/exams/{exam_id}", json=exam, headers=h).status_code == 200
    # 改名：學生與考試的開放班級一起改
    assert client.put("/api/admin/classes/資管一乙", json={"name": "資管一甲"}, headers=h).status_code == 200
    assert client.get(f"/api/admin/exams/{exam_id}", headers=h).json()["class_names"] == "資管一甲"
    roster = {s["student_id"]: s for s in client.get("/api/admin/students", headers=h).json()}
    assert roster["S1"]["class_name"] == "資管一甲"
    s = student_headers(client, "S1")
    assert [e["id"] for e in client.get("/api/exams", headers=s).json()["exams"]] == [exam_id]
    assert client.put("/api/admin/classes/不存在", json={"name": "x"}, headers=h).status_code == 404
    # 刪除班級：學生保留到未分班
    client.post("/api/admin/classes", json={"name": "暫時班"}, headers=h)
    client.post("/api/admin/students", json={"student_id": "S3", "name": "丙", "class_name": "暫時班"}, headers=h)
    r = client.delete("/api/admin/classes/暫時班", headers=h).json()
    assert r["unassigned"] == 1 and r["deleted"] == 0
    assert client.get("/api/admin/classes", headers=h).json()["unassigned"] >= 1
    # 刪除班級連學生：有作答紀錄的保留
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    r = client.delete("/api/admin/classes/資管一甲", params={"students": "delete"}, headers=h).json()
    assert r == {"ok": True, "deleted": 1, "unassigned": 1}
    roster = {x["student_id"]: x for x in client.get("/api/admin/students", headers=h).json()}
    assert "S2" not in roster and roster["S1"]["class_name"] == ""
    assert client.get(f"/api/admin/exams/{exam_id}", headers=h).json()["class_names"] == ""
    assert aid


def test_class_seat_map_shared_by_exams(client):
    h, exam_id = setup_exam(client)
    client.post("/api/admin/classes", json={"name": "資管一乙"}, headers=h)
    for sid, name in (("15130841", "賴郁方"), ("15131586", "鄭紹如")):
        client.post("/api/admin/students", json={"student_id": sid, "name": name, "class_name": "資管一乙"}, headers=h)
    text = "\n".join("\t".join(r) for r in SCHOOL_SHEET)
    v = client.put("/api/admin/classes/資管一乙/seats", json={"seat_map": text}, headers=h).json()
    known = [c["code"] for row in v["grid"] for c in row if c and c["known"]]
    assert sorted(known) == ["15130841", "15131586"]
    assert "12400212李柔萱" in v["unknown"]            # 名單上沒有這位
    assert client.get("/api/admin/classes", headers=h).json()["classes"][0]["seats"] > 0
    # 只開放給資管一乙的考試，座位分析直接用班級座位表；在考試頁修改也會改到班級
    exam = client.get(f"/api/admin/exams/{exam_id}", headers=h).json()
    exam.update(class_names="資管一乙")
    client.put(f"/api/admin/exams/{exam_id}", json=exam, headers=h)
    d = client.get(f"/api/admin/exams/{exam_id}/seats", headers=h).json()
    assert d["owner"] == "資管一乙" and d["seat_map"] == v["seat_map"]
    client.put(f"/api/admin/exams/{exam_id}/seats", json={"seat_map": "15130841,15131586"}, headers=h)
    assert client.get("/api/admin/classes/資管一乙/seats", headers=h).json()["seat_map"] == "15130841,15131586"
    assert client.get("/api/admin/classes/資管一乙/seats", headers=h).json()["unseated"] == []


# ── 依選項比重計分（Moodle 式倒扣）與每場考試的配分 ──

def test_grade_weighted_multi():
    w = [1 / 3, -1 / 6, 1 / 3, 1 / 3]            # CH07.1-28：三個正確各 +33.33%，錯的 -16.67%
    assert grade("multi", [0, 2, 3], [0, 2, 3], 12, weights=w) == pytest.approx(12)
    assert grade("multi", [0, 2, 3], [0, 2], 12, weights=w) == pytest.approx(8)
    assert grade("multi", [0, 2, 3], [0, 1, 2, 3], 12, weights=w) == pytest.approx(10)
    assert grade("multi", [0, 2, 3], [0, 1], 12, weights=w) == pytest.approx(2)
    assert grade("multi", [0, 2, 3], [1], 12, weights=w) == 0     # 最低 0 分，不會變負分


def test_weighted_question_and_exam_points(client):
    h, exam_id = setup_exam(client)
    q = {"type": "multi", "stem": "以下何者有錯？", "options": ["a", "b", "c", "d"], "answer": [0, 2, 3],
         "points": 2, "multi_mode": "weighted", "weights": [33.33333, -16.66667, 33.33333, 33.33333]}
    qid = client.post("/api/admin/questions", json=q, headers=h).json()["id"]
    saved = next(x for x in client.get("/api/admin/questions", headers=h).json() if x["id"] == qid)
    assert saved["multi_mode"] == "weighted" and saved["weights"][1] == pytest.approx(-16.6667, abs=1e-3)
    bad = dict(q, weights=[50, -10, 20, 20])
    assert client.post("/api/admin/questions", json=bad, headers=h).status_code == 400
    bad = dict(q, weights=[50, 50])
    assert client.post("/api/admin/questions", json=bad, headers=h).status_code == 400
    # 這場考試把這題設為 12 分、第 1 題設為 24 分；題庫配分不變
    exam = {"title": "7.1 一維陣列", "published": True, "question_ids": [1, qid],
            "question_points": {"1": 24, str(qid): 12}}
    eid = client.post("/api/admin/exams", json=exam, headers=h).json()["id"]
    got = client.get(f"/api/admin/exams/{eid}", headers=h).json()
    assert got["question_points"] == {"1": 24, str(qid): 12}
    s = student_headers(client, "11201")
    st = client.post(f"/api/exams/{eid}/start", json={}, headers=s).json()
    assert {x["id"]: x["points"] for x in st["questions"]} == {1: 24, qid: 12}
    client.put(f"/api/attempts/{st['attempt_id']}/answers", json={"question_id": qid, "response": [0, 1, 2, 3]}, headers=s)
    r = client.post(f"/api/attempts/{st['attempt_id']}/submit", headers=s).json()
    assert r["score"] == pytest.approx(10) and r["max_score"] == 36
    # 已有人作答後仍可改配分，會重算分數
    got.update(question_points={"1": 24, str(qid): 6})
    assert client.put(f"/api/admin/exams/{eid}", json=got, headers=h).status_code == 200
    sc = client.get(f"/api/admin/exams/{eid}/scores", headers=h).json()
    assert sc["max_score"] == 30 and sc["rows"][0]["score"] == pytest.approx(5)
    assert next(x for x in client.get("/api/admin/questions", headers=h).json() if x["id"] == qid)["points"] == 2
    got.update(question_points={"1": 0})
    assert client.put(f"/api/admin/exams/{eid}", json=got, headers=h).status_code == 400
    # 沒帶配分欄位（例如只改時間）時保留原本的配分
    got.pop("question_points")
    got.update(duration_min=20)
    assert client.put(f"/api/admin/exams/{eid}", json=got, headers=h).status_code == 200
    assert client.get(f"/api/admin/exams/{eid}", headers=h).json()["question_points"] == {"1": 24, str(qid): 6}


# ── 試題分析、成績總表、匯出 Excel ──

def take(client, exam_id, sid, picks):
    """picks：題目 id → 作答。"""
    s = student_headers(client, sid)
    aid = client.post(f"/api/exams/{exam_id}/start", json={}, headers=s).json()["attempt_id"]
    for qid, resp in picks.items():
        client.put(f"/api/attempts/{aid}/answers", json={"question_id": qid, "response": resp}, headers=s)
    client.post(f"/api/attempts/{aid}/submit", headers=s)
    return aid


def test_item_analysis_gradebook_and_csv(client):
    h, exam_id = setup_exam(client, shuffle_opt=False)
    qs = {q["id"]: q for q in client.get("/api/admin/questions", headers=h).json()}
    single = next(q for q in qs.values() if q["type"] == "single")
    right, wrong = single["answer"][0], (single["answer"][0] + 1) % len(single["options"])
    take(client, exam_id, "11201", {single["id"]: right})
    take(client, exam_id, "11202", {single["id"]: right})
    take(client, exam_id, "11203", {single["id"]: wrong})
    d = client.get(f"/api/admin/exams/{exam_id}/items", headers=h).json()
    item = next(i for i in d["items"] if i["id"] == single["id"])
    assert d["count"] == 3 and item["count"] == 3
    assert item["facility"] == pytest.approx(2 / 3)
    assert item["guess"] == pytest.approx(1 / len(single["options"]))
    assert sum(i["intended"] for i in d["items"]) == pytest.approx(1)
    csv_text = client.get(f"/api/admin/exams/{exam_id}/items.csv", headers=h).text
    assert "答對率" in csv_text and "66.67%" in csv_text
    g = client.get(f"/api/admin/exams/{exam_id}/gradebook", headers=h).json()
    assert [r["student_id"] for r in g["rows"]] == ["11201", "11202", "11203"]
    pos = [q["id"] for q in g["questions"]].index(single["id"])
    assert [r["cells"][pos]["state"] for r in g["rows"]] == ["right", "right", "wrong"]
    assert g["rows"][0]["cells"][pos]["response"].startswith("ABCDEF"[right] + ". ")
    assert all(r["cells"][(pos + 1) % 3]["state"] == "blank" for r in g["rows"])


def test_grade_export_follows_gradebook_order(client):
    import base64
    import io
    import openpyxl
    h, exam_id = setup_exam(client)
    client.post("/api/admin/classes", json={"name": "資管一乙"}, headers=h)
    for sid in ("11201", "11202", "11203"):
        client.post("/api/admin/students", json={"student_id": sid, "name": "生" + sid[-1], "class_name": "資管一乙"}, headers=h)
    client.post("/api/admin/students", json={"student_id": "10999", "name": "不在總表", "class_name": "資管一乙"}, headers=h)
    exam = client.get(f"/api/admin/exams/{exam_id}", headers=h).json()
    exam.update(class_names="資管一乙", question_points={"1": 24})
    client.put(f"/api/admin/exams/{exam_id}", json=exam, headers=h)
    take(client, exam_id, "11203", {})
    take(client, exam_id, "10999", {})
    # 助教總表：第 7 列表頭、第 8 列起學生，順序刻意不是學號排序
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet"
    ws.append([])
    ws["A7"], ws["C7"], ws["E7"] = "序號\nCode#", "學號 Stud#", "姓名  Name"
    for n, sid in enumerate(["11203", "11201", "11202"], start=8):
        ws.cell(n, 1, n - 7), ws.cell(n, 3, sid), ws.cell(n, 5, "生" + sid[-1])
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post("/api/admin/classes/資管一乙/grade-order",
                    json={"data": base64.b64encode(buf.getvalue()).decode(), "filename": "總表.xlsx"}, headers=h).json()
    assert r["count"] == 3 and r["extra"] == [{"student_id": "10999", "name": "不在總表"}]
    x = client.get(f"/api/admin/exams/{exam_id}/grades.xlsx", headers=h)
    assert x.status_code == 200 and "spreadsheetml" in x.headers["content-type"]
    out = openpyxl.load_workbook(io.BytesIO(x.content)).active
    assert [out.cell(r, 3).value for r in range(8, 11)] == ["11203", "11201", "11202"]
    assert out.cell(8, 6).value == 0 and out.cell(9, 6).value is None   # 11203 交卷 0 分、11201 沒考留空
    assert out.cell(7, 6).value.startswith("成績/")
    assert "不在成績總表上" in out.cell(12, 1).value and out.cell(13, 3).value == "10999"
    assert client.delete("/api/admin/classes/資管一乙/grade-order", headers=h).json()["count"] == 0


# ── 區分大小寫的填充題 ──

def test_fill_case_sensitive():
    from app.grading import fill_match
    assert fill_match("Double[] students", ["double[] students"]) is True
    assert fill_match("Double[] students", ["double[] students"], case_sensitive=True) is False
    assert fill_match(" double[]  students ", ["double[] students"], case_sensitive=True) is True
    assert grade("fill", [["i<students.length", "i<3"]], ["i<3"], 3, case_sensitive=True) == 3


# ── 操作日誌 ──

def logs(client, h, **params):
    return client.get("/api/admin/logs", params=params, headers=h).json()["rows"]


def test_audit_log_records_admin_actions(client):
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    h, exam_id = setup_exam(client)
    rows = logs(client, h)
    login_ok = [r for r in rows if r["action"] == "後台登入" and r["status"] == 200]
    login_fail = [r for r in rows if r["action"] == "後台登入" and r["status"] == 401]
    assert login_ok and login_fail and login_fail[0]["summary"] == "密碼錯誤"
    # 這次登入之後的操作，工作階段代號和登入那筆相同
    created = next(r for r in rows if r["action"] == "新增考試")
    assert created["session"] and created["session"] == login_ok[0]["session"]
    assert created["target"] == f"考試 #{exam_id}"
    # 修改考試：記下改了哪些欄位的前後值
    exam = client.get(f"/api/admin/exams/{exam_id}", headers=h).json()
    exam.update(duration_min=45, question_points={"1": 20})
    client.put(f"/api/admin/exams/{exam_id}", json=exam, headers=h)
    upd = logs(client, h, action="修改考試")[0]
    assert upd["detail"]["作答時間（分）"] == [30, 45]
    assert upd["detail"]["配分"][1][0] == 20 and "作答時間" in upd["summary"]
    # 下載成績也記錄；一般查詢與未登入的請求不記
    client.get(f"/api/admin/exams/{exam_id}/results.csv", headers=h)
    client.get(f"/api/admin/exams/{exam_id}/monitor", headers=h)
    client.post("/api/admin/classes", json={"name": "x"})
    actions = [r["action"] for r in logs(client, h)]
    assert "下載成績 CSV" in actions and "新增班級" not in actions and not any("monitor" in a for a in actions)
    # 刪除學生、解鎖、搜尋
    client.post("/api/admin/students", json={"student_id": "S9", "name": "九"}, headers=h)
    client.delete("/api/admin/students/S9", headers=h)
    assert logs(client, h, q="S9")[0]["action"] == "刪除學生"
    # 系統指令也能寫進日誌
    from app import audit
    from app.db import connect
    conn = connect()
    audit.write(conn, "匯入題目", "CH07.1-01～05 共 5 題")
    conn.close()
    assert logs(client, h)[0]["actor"] == "系統指令"


# ── 空格不同、多打分號但仍能編譯的程式碼答案算對 ──

def test_fill_ignores_insignificant_spaces_and_statement_semicolons():
    from app.grading import fill_match, statement_blanks
    assert fill_match("students[0] = 5", ["students[0]=5"], case_sensitive=True)
    assert fill_match("double[]students", ["double[] students"], case_sensitive=True)
    assert fill_match("i < students.length", ["i<students.length"], case_sensitive=True)
    assert fill_match("largest num=18,smallest num=2", ["largest num = 18, smallest num = 2"])
    assert not fill_match("doublestudents", ["double students"], case_sensitive=True)   # 英數字之間的空白不能少
    assert not fill_match("largest num = 18 smallest num = 2", ["largest num = 18, smallest num = 2"])  # 少了逗號
    code = "      【1】 ;//宣告\n      【2】 ; //配置\n      for (int 【3】 ;【4】 ;i++)\n         System.out.println( 【5】 );"
    assert statement_blanks(code, 5) == [True, True, False, False, False]
    key = [["double[] students"], ["students=new double[3]"], ["i=0"], ["i<3"], ["students[i]"]]
    full = 5
    assert grade("fill", key, ["double[] students;", "students = new double[3] ;", "i=0", "i<3", "students[i]"],
                 full, case_sensitive=True, statement=statement_blanks(code, 5)) == full
    # for(…)、println(…) 裡多打分號會編譯失敗，不算對
    assert grade("fill", key, ["double[] students", "students=new double[3]", "i=0;", "i<3", "students[i];"],
                 full, case_sensitive=True, statement=statement_blanks(code, 5)) == 3


# ── 管理員帳號 ──

def admin_login_as(client, account, password):
    return client.post("/api/admin/login", json={"account": account, "password": password})


def test_admin_accounts(client):
    h = admin_headers(client)                                     # 共用密碼照常可登入
    assert client.post("/api/admin/admins", json={"account": "15130000", "name": "助教甲"}, headers=h).status_code == 200
    assert client.post("/api/admin/admins", json={"account": "15130000"}, headers=h).status_code == 409
    assert admin_login_as(client, "15130000", "wrong").status_code == 401
    assert admin_login_as(client, "nobody", "mcujava11501").json()["detail"] == "帳號或密碼錯誤"
    r = admin_login_as(client, "15130000", "mcujava11501").json()
    assert r["must_change"] is True and r["name"] == "助教甲"
    ta = {"Authorization": f"Bearer {r['token']}"}
    # 還沒改預設密碼前，除了改密碼之外都不能用
    assert client.get("/api/admin/exams", headers=ta).status_code == 403
    assert client.get("/api/admin/me", headers=ta).json()["must_change"] is True
    bad = client.post("/api/admin/me/password", json={"old_password": "mcujava11501", "new_password": "mcujava11501"}, headers=ta)
    assert bad.status_code == 400
    assert client.post("/api/admin/me/password", json={"old_password": "x", "new_password": "longenough1"}, headers=ta).status_code == 400
    r = client.post("/api/admin/me/password", json={"old_password": "mcujava11501", "new_password": "ta-secret-99"}, headers=ta).json()
    assert client.get("/api/admin/exams", headers=ta).status_code == 401          # 舊 token 失效
    ta = {"Authorization": f"Bearer {r['token']}"}
    assert client.get("/api/admin/exams", headers=ta).status_code == 200
    # 日誌記到帳號
    client.post("/api/admin/classes", json={"name": "資管一乙"}, headers=ta)
    row = next(x for x in logs(client, h) if x["action"] == "新增班級")
    assert row["actor"] == "15130000"
    assert next(x for x in logs(client, h) if x["action"] == "新增管理員")["actor"] == "共用密碼"
    # 不能停用或刪除自己；停用、重設後舊登入失效
    assert client.put("/api/admin/admins/15130000", json={"name": "助教甲", "disabled": True}, headers=ta).status_code == 400
    assert client.delete("/api/admin/admins/15130000", headers=ta).status_code == 400
    client.post("/api/admin/admins/15130000/reset-password", headers=h)
    assert client.get("/api/admin/exams", headers=ta).status_code == 401
    assert admin_login_as(client, "15130000", "mcujava11501").json()["must_change"] is True
    client.put("/api/admin/admins/15130000", json={"name": "助教甲", "disabled": True}, headers=h)
    assert admin_login_as(client, "15130000", "mcujava11501").status_code == 401
    assert [a["account"] for a in client.get("/api/admin/admins", headers=h).json()] == ["15130000"]
    assert client.delete("/api/admin/admins/15130000", headers=h).status_code == 200
    # 共用密碼不能透過網頁改
    assert client.post("/api/admin/me/password", json={"old_password": "teacher", "new_password": "whatever123"}, headers=h).status_code == 400


# ── 模擬作答 ──

def test_admin_preview_does_not_write(client):
    h, exam_id = setup_exam(client, shuffle_opt=True)
    p = client.get(f"/api/admin/exams/{exam_id}/preview", headers=h).json()
    assert p["preview"] and len(p["questions"]) == 3 and all("answer" not in q for q in p["questions"])
    qs = {q["id"]: q for q in client.get("/api/admin/questions", headers=h).json()}
    responses = {}
    for q in p["questions"]:
        key = qs[q["id"]]
        responses[q["id"]] = (key["answer"][0] if key["type"] == "single" else
                              key["answer"] if key["type"] == "multi" else [alts[0] for alts in key["answer"]])
    r = client.post(f"/api/admin/exams/{exam_id}/preview/submit", json={"seed": p["seed"], "responses": responses}, headers=h).json()
    assert r["score"] == r["max_score"] and all("explanation" in q for q in r["questions"])
    # 交卷後檢視的選項順序與作答時一致：正確答案的字母位置對得上
    single = next(q for q in p["questions"] if q["type"] == "single")
    shown = next(q for q in r["questions"] if q["type"] == "single")
    assert [o["text"] for o in single["options"]] == shown["options"]
    from app.db import connect
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0
    conn.close()
    assert client.post(f"/api/admin/exams/{exam_id}/preview/submit", json={"seed": "x", "responses": {}}, headers=h).status_code == 400
    s = student_headers(client, "11201")
    assert client.get(f"/api/admin/exams/{exam_id}/preview", headers=s).status_code == 401
