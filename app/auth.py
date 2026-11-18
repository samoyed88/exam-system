"""簽章 token（學生與老師）與取得真實 IP。"""
import base64
import hashlib
import hmac
import ipaddress
import os
import time

from fastapi import Header, HTTPException, Request

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
        tok_role, subject, exp = payload.split(":", 2)
    except Exception:
        raise HTTPException(401, "登入資訊無效，請重新登入")
    good = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or tok_role != role:
        raise HTTPException(401, "登入資訊無效，請重新登入")
    if int(exp) < time.time():
        raise HTTPException(401, "登入已過期，請重新登入")
    return subject


def _bearer(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "請先登入")
    return authorization[7:]


def current_student(authorization: str = Header("")) -> str:
    return read_token(_bearer(authorization), "s")


def require_admin(authorization: str = Header("")) -> None:
    read_token(_bearer(authorization), "a")


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


# 簡單的猜密碼防護：同一學號 5 分鐘內錯 5 次就先鎖 5 分鐘（各 worker 各自計算）
_failures: dict[str, list[float]] = {}
MAX_FAILURES, LOCK_SECONDS = 5, 300


def login_locked(sid: str) -> bool:
    recent = [t for t in _failures.get(sid, []) if time.time() - t < LOCK_SECONDS]
    _failures[sid] = recent
    return len(recent) >= MAX_FAILURES


def record_failure(sid: str) -> None:
    _failures.setdefault(sid, []).append(time.time())


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
