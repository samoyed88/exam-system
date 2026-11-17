"""SQLite 連線與資料表。每個請求開一條連線；WAL 讓 100 人同時讀寫不互卡。"""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/exam.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    class_name TEXT NOT NULL DEFAULT ''
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
    review_at    TEXT NOT NULL DEFAULT ''            -- review_when = at_time 時的公布時間
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
    finally:
        conn.close()


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
