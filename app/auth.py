"""簽章 token（學生與老師）與取得真實 IP。"""
import base64
import hashlib
import hmac
import ipaddress
import os
import time

from fastapi import Depends, Header, HTTPException, Request

from .db import get_db

_secret = os.getenv("SECRET_KEY", "")
# 程式碼是公開的，不能有預設金鑰，否則任何人都能偽造老師的 token
if len(_secret) < 16 or _secret in ("replace-with-random-hex", "dev-secret-change-me"):
    raise RuntimeError("請在 .env 設定 SECRET_KEY（至少 16 字元的隨機字串）")
SECRET_KEY = _secret.encode()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
STUDENT_TTL = 6 * 3600
ADMIN_TTL = 12 * 3600


def make_token(role: str, subject: str, ttl: int) -> str:
    payload = f"{role}:{subject}:{int(time.time()) + ttl}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()


def read_token(token: str, role: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit(":", 1)
        tok_role, rest = payload.split(":", 1)
        subject, exp = rest.rsplit(":", 1)  # 學號裡有冒號也不會拆錯
        exp = int(exp)
    except Exception:
        raise HTTPException(401, "登入資訊無效，請重新登入")
    good = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode(), good.encode()) or tok_role != role:
        raise HTTPException(401, "登入資訊無效，請重新登入")
    if exp < time.time():
        raise HTTPException(401, "登入已過期，請重新登入")
    return subject


def _bearer(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "請先登入")
    return authorization[7:]


def password_version(password_hash: str) -> str:
    # 放進 token：學生改密碼或老師重設後，舊 token（例如被別人拿去的）立即失效
    return hashlib.sha256(password_hash.encode()).hexdigest()[:12] if password_hash else "-"


def student_token(sid: str, password_hash: str) -> str:
    return make_token("s", f"{sid}|{password_version(password_hash)}", STUDENT_TTL)


def current_student(authorization: str = Header(""), db=Depends(get_db)) -> str:
    sid, _, version = read_token(_bearer(authorization), "s").rpartition("|")
    row = db.execute("SELECT password_hash FROM students WHERE student_id = ?", (sid,)).fetchone()
    if not row:
        raise HTTPException(401, "名單裡已沒有這個學號，請找老師")
    if password_version(row["password_hash"]) != version:
        raise HTTPException(401, "密碼已變更，請重新登入")
    return sid


def admin_subject() -> str:
    # 換掉 ADMIN_PASSWORD 後，舊的老師 token 一併失效
    return hmac.new(SECRET_KEY, b"admin:" + ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()[:16]


def require_admin(authorization: str = Header("")) -> None:
    if read_token(_bearer(authorization), "a") != admin_subject():
        raise HTTPException(401, "登入資訊無效，請重新登入")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), digest)


# 簡單的猜密碼防護：同一個 key 在時間窗內失敗太多次就先擋住。
# key 由呼叫端組合，例如「IP|學號」：只鎖住從那個 IP 來猜的人，校外的人沒辦法故意把學生鎖在門外
_failures: dict[str, list[float]] = {}
MAX_FAILURES, LOCK_SECONDS = 5, 300


def login_locked(key: str, limit: int = MAX_FAILURES, window: int = LOCK_SECONDS) -> bool:
    recent = [t for t in _failures.get(key, []) if time.time() - t < window]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return len(recent) >= limit


def record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.time())


def check_admin_password(password: str) -> bool:
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "伺服器尚未設定 ADMIN_PASSWORD")
    return hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())


def client_ip(request: Request) -> str:
    # 只相信部署方式對應的那個 header，否則學生自己加 header 就能偽造 IP 繞過考場限制。
    # cf：Cloudflare Tunnel 會覆寫 CF-Connecting-IP；xff：Caddy 會把真實 IP 附加在最後一個
    mode = os.getenv("IP_HEADER", "").lower()
    if mode == "cf":
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
    elif mode == "xff":
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


def ip_allowed(ip: str, allowed: str) -> bool:
    rules = [r.strip() for r in allowed.replace(",", "\n").splitlines() if r.strip()]
    if not rules:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for rule in rules:
        try:
            if addr in ipaddress.ip_network(rule, strict=False):
                return True
        except ValueError:
            continue
    return False
