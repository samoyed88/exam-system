"""在伺服器上建立或重設管理員帳號（共用密碼登入已停用，最高管理員忘記密碼時用這個救回）。

密碼一律設為 .env 的 ADMIN_DEFAULT_PASSWORD，下次登入必須修改；操作會記進後台日誌（系統指令）。

用法（Docker 內；先停 app，避免兩個行程同時寫 SQLite，見 Dockerfile）：
    docker compose stop app
    docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts:ro" app \\
        python -m scripts.admin_cli 12130383 --name 何柏諺 --owner
    docker compose start app
"""
import argparse
from datetime import datetime, timezone

from app import audit
from app.db import connect, init_db
from app.main import ADMIN_DEFAULT_PASSWORD
from app.auth import hash_password


def main():
    ap = argparse.ArgumentParser(description="建立或重設管理員帳號（密碼改回預設密碼）")
    ap.add_argument("account")
    ap.add_argument("--name", default="")
    ap.add_argument("--owner", action="store_true", help="設為最高管理員")
    a = ap.parse_args()
    if not ADMIN_DEFAULT_PASSWORD:
        raise SystemExit("請先在 .env 設定 ADMIN_DEFAULT_PASSWORD")
    init_db()
    db = connect()
    db.execute("BEGIN IMMEDIATE")
    old = db.execute("SELECT * FROM admins WHERE account = ?", (a.account,)).fetchone()
    role = "owner" if a.owner else (old["role"] if old else "admin")
    name = a.name or (old["name"] if old else "")
    if old:
        db.execute("UPDATE admins SET name = ?, password_hash = ?, must_change = 1, disabled = 0, role = ? WHERE account = ?",
                   (name, hash_password(ADMIN_DEFAULT_PASSWORD), role, a.account))
    else:
        db.execute("INSERT INTO admins (account, name, password_hash, must_change, created_at, role) VALUES (?, ?, ?, 1, ?, ?)",
                   (a.account, name, hash_password(ADMIN_DEFAULT_PASSWORD), datetime.now(timezone.utc).isoformat(), role))
    label = "最高管理員" if role == "owner" else "一般管理員"
    audit.write(db, "重設管理員密碼" if old else "新增管理員",
                f"{a.account} {name}，{label}（預設密碼，下次登入須修改）", target=f"管理員 {a.account}")
    db.execute("COMMIT")
    print(("已重設" if old else "已建立") + f" {a.account} {name}（{label}），密碼為預設密碼，下次登入須修改")


if __name__ == "__main__":
    main()
