"""產生一場「模擬考試」的成績與作答紀錄，讓老師先看成績統計、作答紀錄的效果。

會建立：模擬班學生（學號 DEMO 開頭）與一場未發布的考試「【模擬】07 陣列 小考」，
從題庫挑題，依每位學生的程度模擬作答、改答、離開畫面、停留時間、時間到自動交卷。
考試不發布、只開放給模擬班，真正的學生看不到。重跑會先刪掉上一次的模擬資料。

用法（Docker 內；先停 app，避免兩個行程同時寫 SQLite，見 Dockerfile）：
    docker compose stop app
    docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts:ro" app python -m scripts.seed_demo
    docker compose start app
只刪除模擬資料：最後一行的指令加 --remove
"""
import argparse
import json
import math
import random
from datetime import datetime, timedelta, timezone

from app.db import connect, init_db
from app.main import TZ, log_event, score_answer, student_questions

TITLE = "【模擬】07 陣列 小考"
CLASS = "模擬班"
PREFIX = "DEMO"
SURNAMES = "陳林黃張李王吳劉蔡楊許鄭謝洪郭邱曾廖賴徐周葉蘇莊呂江何蕭羅高"
GIVEN = "承宇冠廷宥辰柏翰品妤家瑜子晴思妍彥廷宇恩詠晴俊傑佳穎睿哲語彤昱安祐誠芷涵"


def remove(db) -> None:
    db.execute("DELETE FROM exams WHERE title = ?", (TITLE,))  # 連同作答、紀錄一起刪
    db.execute("DELETE FROM students WHERE class_name = ? AND student_id LIKE ?",
               (CLASS, PREFIX + "%"))


def pick_questions(db, rng) -> list[int]:
    out = []
    for kind, n in (("single", 18), ("multi", 4), ("fill", 4)):
        ids = [r[0] for r in db.execute("SELECT id FROM questions WHERE type = ? ORDER BY id", (kind,))]
        out += sorted(rng.sample(ids, min(n, len(ids))))
    return out


def wrong_fill(text: str, rng) -> str:
    if len(text) > 2 and rng.random() < .5:
        i = rng.randrange(len(text) - 1)
        return text[:i] + text[i + 1] + text[i] + text[i + 2:]  # 兩個字元順序錯
    return rng.choice(["Error", text[:-1], text + text[-1]])


def choose(q, correct: bool, rng):
    answer = json.loads(q["answer"])
    n = len(json.loads(q["options"]))
    if q["type"] == "fill":
        return [b[0] if correct or rng.random() < .3 else wrong_fill(b[0], rng) for b in answer]
    if q["type"] == "single":
        return answer[0] if correct else rng.choice([i for i in range(n) if i != answer[0]])
    if correct:
        return sorted(answer)
    picks = set(answer)
    if len(picks) > 1 and rng.random() < .6:
        picks.discard(rng.choice(sorted(picks)))           # 漏選一個
    else:
        picks.add(rng.choice([i for i in range(n) if i not in picks] or [0]))  # 多選一個
    return sorted(picks)


def simulate(db, exam, sid: str, n: int, rng, difficulty: dict, device: str) -> None:
    theta = rng.gauss(0, 1)                                   # 程度
    speed = math.exp(rng.gauss(0, .28) - .12 * theta)        # 越大越慢
    start_at = datetime.fromisoformat(exam["start_at"]).replace(tzinfo=TZ).astimezone(timezone.utc)
    end_at = datetime.fromisoformat(exam["end_at"]).replace(tzinfo=TZ).astimezone(timezone.utc)
    started = start_at + timedelta(seconds=rng.uniform(5, 200))
    deadline = min(started + timedelta(minutes=exam["duration_min"]), end_at)
    ip = f"10.20.1.{n + 10}"
    cur = db.execute(
        "INSERT INTO attempts (exam_id, student_id, started_at, deadline, ip, device) VALUES (?, ?, ?, ?, ?, ?)",
        (exam["id"], sid, started.isoformat(), deadline.isoformat(), ip, device))
    aid = cur.lastrowid
    events = [(started, "start", None, f"IP {ip}，電腦 {device}")]
    qs = student_questions(db, exam, sid)
    t = started + timedelta(seconds=rng.uniform(3, 15))      # 先看一下題目
    final, dwell, timeouts = {}, {}, False
    base = {"single": 38, "multi": 65, "fill": 85}

    def answer(q, resp, at):
        if final.get(q["id"]) == resp:
            return
        final[q["id"]] = resp
        events.append((at, "answer", q["id"], json.dumps(resp, ensure_ascii=False)))

    def p_correct(q):
        return 1 / (1 + math.exp(-1.7 * (theta - difficulty[q["id"]])))

    # 第一輪：依序作答
    skipped = []
    for q in qs:
        sec = base[q["type"]] * speed * (1 + .3 * difficulty[q["id"]]) * math.exp(rng.gauss(0, .35))
        sec = max(6, sec)
        if theta < -.3 and rng.random() < .08:               # 不會的先跳過
            sec = rng.uniform(5, 15)
            skipped.append(q)
        else:
            ok = rng.random() < p_correct(q)
            if q["type"] == "single" and rng.random() < .1:  # 先選一個，想一想又改
                answer(q, choose(q, not ok, rng), t + timedelta(seconds=sec * .5))
            answer(q, choose(q, ok, rng), t + timedelta(seconds=sec))
        dwell[q["id"]] = dwell.get(q["id"], 0) + sec
        t += timedelta(seconds=sec + rng.uniform(1, 4))
    # 第二輪：回頭看跳過的題目與幾題沒把握的
    revisit = skipped + rng.sample(qs, rng.choice([0, 0, 1, 2, 3, 4]))
    for q in revisit:
        sec = rng.uniform(10, 50) * speed
        if q in skipped or rng.random() < .4:
            answer(q, choose(q, rng.random() < p_correct(q), rng), t + timedelta(seconds=sec))
        dwell[q["id"]] = dwell.get(q["id"], 0) + sec
        t += timedelta(seconds=sec + 2)
    submit_at = t + timedelta(seconds=rng.uniform(8, 40))    # 作答摘要頁確認
    if submit_at > deadline:                                  # 時間到：只採計到期前的作答
        timeouts = True
        ratio = (deadline - started) / (t - started)          # 停留時間也只算到時間到為止
        dwell = {k: v * ratio for k, v in dwell.items()}
        submit_at = deadline + timedelta(seconds=rng.uniform(1, 5))
    # 離開畫面：多數人 0 次，少數人好幾次
    blurs = rng.choices([0, 1, 2, 5], weights=[75, 13, 8, 4])[0]
    for _ in range(blurs):
        away = started + (submit_at - started) * rng.uniform(.1, .9)
        events.append((away, "blur", None, ""))
        events.append((away + timedelta(seconds=rng.uniform(4, 70)), "focus", None, ""))

    events = sorted((e for e in events if e[0] <= submit_at), key=lambda e: e[0])
    last = {}
    for at, kind, qid, detail in events:
        log_event(db, aid, kind, qid, detail, at=at)
        if kind == "answer":
            last[qid] = (detail, at)
    log_event(db, aid, "auto_submit" if timeouts else "submit", at=submit_at)
    total = max_total = 0.0
    for q in qs:
        max_total += q["points"]
        if q["id"] in last:
            detail, at = last[q["id"]]
            s = score_answer(q, json.loads(detail))
            total += s
            db.execute("INSERT INTO answers (attempt_id, question_id, response, score, updated_at) "
                       "VALUES (?, ?, ?, ?, ?)", (aid, q["id"], detail, s, at.isoformat()))
        if q["id"] in dwell:
            db.execute("INSERT INTO question_time VALUES (?, ?, ?)", (aid, q["id"], round(dwell[q["id"]])))
    db.execute("UPDATE attempts SET submitted_at = ?, score = ?, max_score = ?, tab_switches = ? WHERE id = ?",
               (submit_at.isoformat(), round(total, 2), round(max_total, 2), blurs, aid))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--students", type=int, default=48)
    ap.add_argument("--absent", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()
    init_db()
    db = connect()
    rng = random.Random(a.seed)
    db.execute("BEGIN IMMEDIATE")
    try:
        remove(db)
        if a.remove:
            db.execute("COMMIT")
            print("已刪除模擬資料")
            return
        qids = pick_questions(db, rng)
        if not qids:
            raise SystemExit("題庫是空的，請先匯入題目")
        day = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        cur = db.execute(
            "INSERT INTO exams (title, start_at, end_at, duration_min, class_names, show_mode, "
            "review_when, published) VALUES (?, ?, ?, 30, ?, 'explain', 'after_close', 0)",
            (TITLE, f"{day}T10:10", f"{day}T10:45", CLASS))
        exam_id = cur.lastrowid
        db.executemany("INSERT INTO exam_questions VALUES (?, ?, ?)",
                       [(exam_id, q, i) for i, q in enumerate(qids)])
        exam = db.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
        difficulty = {q: rng.gauss(-.3, .9) for q in qids}
        names = set()
        while len(names) < a.students:
            names.add(rng.choice(SURNAMES) + rng.choice(GIVEN) + rng.choice(GIVEN))
        sids = [f"{PREFIX}{i + 1:03d}" for i in range(a.students)]
        db.executemany("INSERT INTO students (student_id, name, class_name) VALUES (?, ?, ?)",
                       [(s, nm, CLASS) for s, nm in zip(sids, sorted(names))])
        absent = set(rng.sample(sids, a.absent))
        for n, sid in enumerate(sids):
            if sid in absent:
                continue
            # 製造一組「同一台電腦登入兩個學號」讓監控頁有警示可看
            device = "PC17" if sid == f"{PREFIX}023" else f"PC{n + 1:02d}"
            simulate(db, exam, sid, n, rng, difficulty, device)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    rows = db.execute("SELECT score, max_score FROM attempts WHERE exam_id = ?", (exam_id,)).fetchall()
    avg = sum(r[0] for r in rows) / len(rows)
    print(f"已建立考試 #{exam_id}「{TITLE}」：{len(qids)} 題、{len(rows)} 人交卷、"
          f"{a.absent} 人缺考，平均 {avg:.1f} / {rows[0][1]}")


if __name__ == "__main__":
    main()
