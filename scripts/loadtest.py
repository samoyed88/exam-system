"""壓力測試：模擬 N 位學生同時登入、作答、交卷（或時間到由伺服器代交）。

會在目標伺服器建立測試學生、題目與考試，請只對測試用的站台執行，不要打正式資料庫。

用法：
    python scripts/loadtest.py --url http://127.0.0.1:8013 --password <後台密碼> \
        --students 100 --questions static/samples/questions.csv
    加 --auto-submit 改測「時間到伺服器代交」：考試限時 1 分鐘、學生不按交卷。
"""
import argparse
import asyncio
import csv
import io
import random
import statistics
import time
from collections import defaultdict

import httpx

lat = defaultdict(list)    # endpoint -> 秒
errors = defaultdict(int)  # "endpoint status" -> 次數


async def req(client, name, method, url, **kw):
    for attempt in range(3):  # 學生端遇到網路錯誤也會重送
        t = time.perf_counter()
        try:
            r = await client.request(method, url, **kw)
        except httpx.HTTPError as e:
            errors[f"{name} {type(e).__name__}"] += 1
            await asyncio.sleep(1)
            continue
        lat[name].append(time.perf_counter() - t)
        if r.status_code >= 400:
            errors[f"{name} {r.status_code}"] += 1
        return r
    return None


def random_response(q):
    if q["type"] == "single":
        return random.randrange(len(q["options"]))
    if q["type"] == "multi":
        return random.sample(range(len(q["options"])), random.randint(1, len(q["options"])))
    return [random.choice(["8", "Error", "81216", "abc"]) for _ in range(q["blanks"])]


async def student(client, sid, exam_id, start_gate, submit_gate, think, submit):
    await start_gate.wait()
    await asyncio.sleep(random.uniform(0, 3))  # 開考後幾秒內陸續進場
    r = await req(client, "login", "POST", "/api/login", json={"student_id": sid})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    await req(client, "exams", "GET", "/api/exams", headers=h)
    for _ in range(3):  # 失敗時學生會再按一次「開始作答」
        r = await req(client, "start", "POST", f"/api/exams/{exam_id}/start",
                      json={"device": f"PC{sid[-3:]}"}, headers=h)
        if r is not None and r.status_code == 200:
            break
        await asyncio.sleep(1)
    started = r.json()
    aid = started["attempt_id"]
    qs = started["questions"]
    random.shuffle(qs)
    for q in qs:
        await asyncio.sleep(random.uniform(0, think))
        await req(client, "save", "PUT", f"/api/attempts/{aid}/answers",
                  json={"question_id": q["id"], "response": random_response(q)}, headers=h)
        if random.random() < .15:  # 改答案
            await req(client, "save", "PUT", f"/api/attempts/{aid}/answers",
                      json={"question_id": q["id"], "response": random_response(q)}, headers=h)
    if not submit:
        return aid
    submit_gate.append(1)
    while len(submit_gate) < submit_gate.maxlen:  # 全部人作答完才一起按交卷
        await asyncio.sleep(.05)
    await req(client, "submit", "POST", f"/api/attempts/{aid}/submit", headers=h)
    await req(client, "result", "GET", f"/api/attempts/{aid}/result", headers=h)
    return aid


def report(title, wall):
    print(f"\n== {title}（{wall:.1f} 秒）")
    print(f"{'API':<8}{'次數':>7}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'max ms':>9}")
    for name, v in lat.items():
        v = sorted(v)
        pick = lambda p: v[min(len(v) - 1, int(len(v) * p))] * 1000
        print(f"{name:<8}{len(v):>7}{statistics.median(v) * 1000:>9.0f}{pick(.95):>9.0f}"
              f"{pick(.99):>9.0f}{v[-1] * 1000:>9.0f}")
    print("錯誤：", dict(errors) if errors else "無")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--students", type=int, default=100)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--think", type=float, default=0.5, help="每題作答前最多等幾秒")
    ap.add_argument("--auto-submit", action="store_true")
    a = ap.parse_args()

    limits = httpx.Limits(max_connections=a.students * 2, max_keepalive_connections=a.students * 2)
    async with httpx.AsyncClient(base_url=a.url, timeout=30, limits=limits) as client:
        token = (await client.post("/api/admin/login", json={"password": a.password})).json()["token"]
        ah = {"Authorization": f"Bearer {token}"}
        prefix = str(random.randint(100, 999))
        sids = [f"LT{prefix}{i:03d}" for i in range(a.students)]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["學號", "姓名"])
        w.writerows([s, f"壓測{s[-3:]}"] for s in sids)
        r = await client.post("/api/admin/students/import", json={"csv": buf.getvalue()}, headers=ah)
        r.raise_for_status()
        before = {q["id"] for q in (await client.get("/api/admin/questions", headers=ah)).json()}
        r = await client.post("/api/admin/questions/import",
                              json={"csv": open(a.questions, encoding="utf-8-sig").read()}, headers=ah)
        r.raise_for_status()
        qids = sorted({q["id"] for q in (await client.get("/api/admin/questions", headers=ah)).json()} - before)
        exam = {"title": f"壓力測試 {a.students} 人" + ("（時間到代交）" if a.auto_submit else ""),
                "duration_min": 1 if a.auto_submit else 60, "published": True,
                "show_mode": "explain", "question_ids": qids}
        exam_id = (await client.post("/api/admin/exams", json=exam, headers=ah)).json()["id"]
        print(f"考試 #{exam_id}：{len(qids)} 題，{a.students} 位學生")

        gate = asyncio.Event()
        from collections import deque
        submit_gate = deque(maxlen=a.students)
        tasks = [asyncio.create_task(student(client, s, exam_id, gate, submit_gate, a.think,
                                             not a.auto_submit)) for s in sids]
        t0 = time.perf_counter()
        gate.set()
        await asyncio.gather(*tasks)
        report("作答" + ("" if a.auto_submit else "＋同時交卷"), time.perf_counter() - t0)

        if a.auto_submit:
            print("\n等待時間到由伺服器代交…")
            while True:
                await asyncio.sleep(5)
                mon = (await client.get(f"/api/admin/exams/{exam_id}/monitor", headers=ah)).json()
                left = sum(1 for r in mon["rows"] if r["status"] == "in_progress")
                print(f"  {time.perf_counter() - t0:5.0f} 秒：作答中 {left} 人")
                if not left:
                    break

        t = time.perf_counter()
        sc = (await client.get(f"/api/admin/exams/{exam_id}/scores", headers=ah)).json()
        t_scores = time.perf_counter() - t
        t = time.perf_counter()
        mon = (await client.get(f"/api/admin/exams/{exam_id}/monitor", headers=ah)).json()
        t_mon = time.perf_counter() - t
        rows = [r for r in mon["rows"] if r["student_id"] in set(sids)]
        done = [r for r in rows if r["status"] == "submitted"]
        full = [r for r in done if r["answered"] == len(qids)]
        print(f"\n驗證：已交卷 {len(done)}/{a.students}，答案完整 {len(full)}/{a.students}，"
              f"平均 {sc['summary'].get('mean')} / {sc['max_score']}")
        print(f"後台 monitor {t_mon * 1000:.0f} ms、成績統計 {t_scores * 1000:.0f} ms")
        ok = len(full) == a.students and not errors
        print("結果：", "通過 ✅" if ok else "有問題 ❌")


if __name__ == "__main__":
    asyncio.run(main())
