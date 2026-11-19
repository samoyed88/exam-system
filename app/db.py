"""SQLite 連線與資料表。每個請求開一條連線；WAL 讓 100 人同時讀寫不互卡（限單一行程，見 Dockerfile）。"""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/exam.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    class_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT ''  -- 空 = 尚未設定密碼，只輸入學號即可登入
);

-- 後台操作日誌（見 app/audit.py）
CREATE TABLE IF NOT EXISTS admin_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,              -- 後台／系統指令
    ip      TEXT NOT NULL DEFAULT '',
    device  TEXT NOT NULL DEFAULT '',   -- 瀏覽器／作業系統
    session TEXT NOT NULL DEFAULT '',   -- 登入工作階段代號（同一次登入相同）
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '{}', -- JSON：修改前後的內容
    status  INTEGER NOT NULL DEFAULT 200,
    method  TEXT NOT NULL DEFAULT '',
    path    TEXT NOT NULL DEFAULT ''
);

-- 班級：學生以 students.class_name 歸班；這裡放班級本身與座位表（同班每場考試共用）
CREATE TABLE IF NOT EXISTS classes (
    name     TEXT PRIMARY KEY,
    seat_map TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN ('single', 'multi', 'fill')),
    stem        TEXT NOT NULL,
    code        TEXT NOT NULL DEFAULT '',
    options     TEXT NOT NULL DEFAULT '[]',   -- JSON：選項文字
    answer      TEXT NOT NULL,                -- JSON：single [i]、multi [i,j]、fill [["臺北","台北"], ...]
    explanation TEXT NOT NULL DEFAULT '',
    points      REAL NOT NULL DEFAULT 1,
    multi_mode  TEXT NOT NULL DEFAULT 'all' CHECK (multi_mode IN ('all', 'partial')),
    tags        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS exams (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    start_at     TEXT NOT NULL DEFAULT '',     -- ISO 時間，空字串 = 不限
    end_at       TEXT NOT NULL DEFAULT '',
    duration_min INTEGER NOT NULL DEFAULT 50,
    class_names  TEXT NOT NULL DEFAULT '',     -- 逗號分隔，空 = 所有班
    shuffle_q    INTEGER NOT NULL DEFAULT 1,
    shuffle_opt  INTEGER NOT NULL DEFAULT 1,
    show_mode    TEXT NOT NULL DEFAULT 'score' CHECK (show_mode IN ('score', 'answers', 'explain')),
    allowed_ips  TEXT NOT NULL DEFAULT '',     -- 換行分隔 IP 或 CIDR，空 = 不限
    published    INTEGER NOT NULL DEFAULT 0,
    review_when  TEXT NOT NULL DEFAULT 'immediate',  -- immediate 交卷後立即、after_close 結束後、at_time 指定時間
    review_at    TEXT NOT NULL DEFAULT '',           -- review_when = at_time 時的公布時間
    allow_review INTEGER NOT NULL DEFAULT 1,         -- 0 = 只在交卷當下看一次詳解，之後只看總分
    review_until TEXT NOT NULL DEFAULT '',           -- 可回頭複習的截止時間，空 = 不限
    focus_mode   TEXT NOT NULL DEFAULT 'record',     -- 離開考試畫面：record 只記錄、warn 警告、lock 超過次數鎖定、submit 超過次數交卷
    focus_limit  INTEGER NOT NULL DEFAULT 3,         -- lock / submit 允許離開的次數
    fullscreen   INTEGER NOT NULL DEFAULT 0,         -- 1 = 要求全螢幕作答，離開全螢幕算一次離開
    block_copy   INTEGER NOT NULL DEFAULT 0,         -- 1 = 禁止複製、貼上、右鍵
    seat_map     TEXT NOT NULL DEFAULT ''            -- 座位表：每行一排，逗號或 Tab 分隔電腦代碼／學號，空格 = 走道
);

CREATE TABLE IF NOT EXISTS exam_questions (
    exam_id     INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    position    INTEGER NOT NULL,
    PRIMARY KEY (exam_id, question_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id      INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    student_id   TEXT NOT NULL REFERENCES students(student_id),
    started_at   TEXT NOT NULL,
    deadline     TEXT NOT NULL,
    submitted_at TEXT,
    score        REAL,
    max_score    REAL,
    ip           TEXT NOT NULL DEFAULT '',
    device       TEXT NOT NULL DEFAULT '',
    tab_switches INTEGER NOT NULL DEFAULT 0,
    locked       INTEGER NOT NULL DEFAULT 0,      -- 1 = 離開畫面超過次數，等老師解鎖
    UNIQUE (exam_id, student_id)
);

CREATE TABLE IF NOT EXISTS answers (
    attempt_id  INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL,
    response    TEXT NOT NULL,                -- JSON
    score       REAL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (attempt_id, question_id)
);

-- 作答紀錄：start 進場、resume 重新進入、answer 作答、blur 離開畫面、focus 回到畫面、
-- submit 交卷、auto_submit 時間到代交、force_submit 老師代交
CREATE TABLE IF NOT EXISTS attempt_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id  INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    question_id INTEGER,
    detail      TEXT NOT NULL DEFAULT ''      -- answer 為作答 JSON，start/resume 為 IP 與電腦
);
CREATE INDEX IF NOT EXISTS attempt_log_attempt ON attempt_log (attempt_id, id);

-- 每題停留秒數：學生端統計畫面停在該題且視窗在前景的時間，定時回報累加
CREATE TABLE IF NOT EXISTS question_time (
    attempt_id  INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL,
    seconds     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (attempt_id, question_id)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 舊資料庫補欄位：(表, 欄位, 定義)
MIGRATIONS = [
    ("exams", "review_when", "TEXT NOT NULL DEFAULT 'immediate'"),
    ("exams", "review_at", "TEXT NOT NULL DEFAULT ''"),
    ("exams", "allow_review", "INTEGER NOT NULL DEFAULT 1"),
    ("exams", "review_until", "TEXT NOT NULL DEFAULT ''"),
    ("students", "password_hash", "TEXT NOT NULL DEFAULT ''"),
    ("exams", "focus_mode", "TEXT NOT NULL DEFAULT 'record'"),
    ("exams", "focus_limit", "INTEGER NOT NULL DEFAULT 3"),
    ("exams", "fullscreen", "INTEGER NOT NULL DEFAULT 0"),
    ("exams", "block_copy", "INTEGER NOT NULL DEFAULT 0"),
    ("exams", "seat_map", "TEXT NOT NULL DEFAULT ''"),
    ("attempts", "locked", "INTEGER NOT NULL DEFAULT 0"),
    # 多選依選項比重計分（Moodle 式，可倒扣）：JSON 各選項比重，例如 [0.3333, -0.1667, ...]；
    # 有值時優先於 multi_mode（multi_mode 的 CHECK 限制改不了，API 對外稱為 weighted）
    ("questions", "weights", "TEXT NOT NULL DEFAULT ''"),
    ("exam_questions", "points", "REAL"),          # 這場考試的配分；NULL = 用題庫預設配分
    ("classes", "grade_order", "TEXT NOT NULL DEFAULT ''"),  # 助教成績總表的學號順序（JSON），匯出時照這個順序
    ("questions", "case_sensitive", "INTEGER NOT NULL DEFAULT 0"),  # 填充題比對是否區分大小寫（Moodle SHORTANSWER_C）
]


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                except sqlite3.OperationalError:
                    pass  # 另一個 worker 同時補過了
        # 舊資料只有學生身上的班級名稱，補進班級表
        conn.execute("INSERT OR IGNORE INTO classes (name) "
                     "SELECT DISTINCT class_name FROM students WHERE class_name != ''")
    finally:
        conn.close()


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
