import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["ADMIN_PASSWORD"] = "teacher"
os.environ["SECRET_KEY"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.grading import grade  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "exam.db")
    monkeypatch.delenv("IP_HEADER", raising=False)
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
    exams = client.get("/api/exams", headers=s).json()
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
    assert client.get("/api/exams", headers=student_headers(client, "11203")).json() == []
    assert client.post(f"/api/exams/{exam_id}/start", json={},
                       headers=student_headers(client, "11203")).status_code == 403
    _, hidden = setup_exam(client, published=False)
    ids = [e["id"] for e in client.get("/api/exams", headers=student_headers(client, "11201")).json()]
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
