"""Web control plane for the Telegram -> QQ forwarder.

The API owns users and forwarding routes in SQLite.  The bridge runtime uses
the same Telethon and OneBot helpers as ``bridge.py`` and updates handlers as
routes are created or disabled from the panel.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import APIKeyCookie
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import TelegramClient, errors, events, utils
from telethon.tl.types import Channel, Chat, User

from bridge import Route, build_segments, onebot_call, send_to_qq, tg_dialog_kind


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
PANEL_DB = Path(os.getenv("PANEL_DB", str(ROOT / "data" / "panel.db")))
TG_SESSION_DIR = Path(os.getenv("TG_SESSION_DIR", str(ROOT / "data" / "sessions")))
PANEL_SECRET = os.getenv("PANEL_SECRET") or secrets.token_urlsafe(32)
SESSION_COOKIE = os.getenv("PANEL_SESSION_COOKIE", "tgqq_session")
SESSION_SECONDS = int(os.getenv("PANEL_SESSION_SECONDS", "604800"))
COOKIE_SECURE = os.getenv("PANEL_COOKIE_SECURE", "false").lower() == "true"
logger = logging.getLogger("tg-qq-panel")


def db_connect() -> sqlite3.Connection:
    PANEL_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(PANEL_DB, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tg_chat_id INTEGER NOT NULL,
                tg_title TEXT NOT NULL,
                qq_type TEXT NOT NULL CHECK (qq_type IN ('private', 'group')),
                qq_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_routes_enabled ON routes(enabled);
            CREATE TABLE IF NOT EXISTS message_links (
                qq_message_id INTEGER PRIMARY KEY,
                route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
                owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tg_chat_id INTEGER NOT NULL,
                tg_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_message_links_owner ON message_links(owner_user_id);
            """
        )

        admin_username = os.getenv("ADMIN_USERNAME")
        admin_password = os.getenv("ADMIN_PASSWORD")
        if admin_username and admin_password:
            exists = db.execute("SELECT id FROM users WHERE username = ?", (admin_username,)).fetchone()
            if exists is None:
                db.execute(
                    "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'admin')",
                    (admin_username, hash_password(admin_password)),
                )
                logger.info("Created initial admin user %s", admin_username)
        elif not db.execute("SELECT id FROM users LIMIT 1").fetchone():
            logger.warning("No users exist. Set ADMIN_USERNAME and ADMIN_PASSWORD before logging in.")

        admin_row = db.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
        legacy_value = os.getenv("TG_SESSION")
        if admin_row and legacy_value:
            legacy_base = Path(legacy_value)
            if not legacy_base.is_absolute():
                legacy_base = ROOT / legacy_base
            legacy_file = legacy_base if legacy_base.suffix == ".session" else legacy_base.with_suffix(".session")
            target_base = TG_SESSION_DIR / f"user_{int(admin_row['id'])}"
            target_file = target_base.with_suffix(".session")
            if legacy_file.exists() and not target_file.exists():
                TG_SESSION_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_file, target_file)
                logger.info("Migrated legacy Telegram session to %s", target_file)


def hash_password(password: str, salt: bytes | None = None) -> str:
    if not password or len(password) < 8:
        raise ValueError("Password must contain at least 8 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def issue_session(user_id: int) -> str:
    expires = int(time.time()) + SESSION_SECONDS
    payload = f"{user_id}.{expires}".encode("ascii")
    signature = hmac.new(PANEL_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def session_user_id(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    payload_encoded, signature_encoded = token.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_encoded + "===")
        signature = base64.urlsafe_b64decode(signature_encoded + "===")
        expected = hmac.new(PANEL_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        user_id_text, expires_text = payload.decode("ascii").split(".", 1)
        if int(expires_text) < int(time.time()):
            return None
        return int(user_id_text)
    except (ValueError, UnicodeDecodeError, TypeError):
        return None


def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


def public_route(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_user_id": row["owner_user_id"],
        "tg_chat_id": row["tg_chat_id"],
        "tg_title": row["tg_title"],
        "qq_type": row["qq_type"],
        "qq_id": row["qq_id"],
        "enabled": bool(row["enabled"]),
        "last_error": row["last_error"],
        "created_at": row["created_at"],
    }


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    with db_connect() as db:
        return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)


def current_user(session: str | None = Depends(cookie_scheme)) -> dict[str, Any]:
    user_id = session_user_id(session)
    row = get_user_by_id(user_id) if user_id else None
    if row is None or not row["active"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    return public_user(row)


def admin_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class RoutePayload(BaseModel):
    tg_chat_id: int | None = None
    tg_title: str = Field(default="", max_length=200)
    tg_username: str | None = Field(default=None, max_length=200)
    qq_type: Literal["private", "group"]
    qq_id: int
    enabled: bool = True


class RouteUpdate(BaseModel):
    enabled: bool | None = None
    qq_type: Literal["private", "group"] | None = None
    qq_id: int | None = None


class UserPayload(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)
    role: Literal["admin", "user"] = "user"


class UserUpdate(BaseModel):
    active: bool | None = None
    role: Literal["admin", "user"] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class TelegramCodePayload(BaseModel):
    phone: str = Field(min_length=5, max_length=32)


class TelegramVerifyPayload(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=16)
    password: str | None = Field(default=None, min_length=1, max_length=256)


class UserTelegramRuntime:
    """One isolated Telethon client and session for one panel user."""

    def __init__(self, manager: "MultiUserBridgeRuntime", user_id: int) -> None:
        self.manager = manager
        self.user_id = user_id
        self.client: TelegramClient | None = None
        self.handlers: dict[int, Any] = {}
        self.ready = False
        self.error: str | None = None
        self.auth_phone: str | None = None
        self.auth_phone_code_hash: str | None = None
        self.auth_lock = asyncio.Lock()

    @property
    def session_path(self) -> Path:
        return TG_SESSION_DIR / f"user_{self.user_id}"

    async def start(self) -> None:
        try:
            api_id = int(os.environ["TG_API_ID"])
            api_hash = os.environ["TG_API_HASH"]
            TG_SESSION_DIR.mkdir(parents=True, exist_ok=True)
            TG_SESSION_DIR.chmod(0o700)
            self.client = TelegramClient(str(self.session_path), api_id, api_hash)
            await self.client.connect()
            session_file = self.session_path.with_suffix(".session")
            if session_file.exists():
                session_file.chmod(0o600)
            if await self.client.is_user_authorized():
                await self.activate()
            else:
                self.error = "Telegram 尚未授权"
                logger.info("Telegram user %s is not authorized", self.user_id)
        except Exception as exc:
            self.error = str(exc)
            logger.exception("Telegram runtime unavailable for user %s", self.user_id)

    async def activate(self) -> None:
        self.ready = True
        self.error = None
        with db_connect() as db:
            rows = db.execute(
                "SELECT * FROM routes WHERE owner_user_id = ? AND enabled = 1 ORDER BY id",
                (self.user_id,),
            ).fetchall()
        for row in rows:
            await self.add_route(row)
        logger.info("Telegram runtime ready for panel user %s", self.user_id)

    async def send_login_code(self, phone: str) -> dict[str, Any]:
        if not self.client:
            await self.start()
        if not self.client:
            raise RuntimeError(self.error or "Telegram 客户端未初始化")
        async with self.auth_lock:
            if not self.client.is_connected():
                await self.client.connect()
            sent = await self.client.send_code_request(phone)
            self.auth_phone = phone
            self.auth_phone_code_hash = sent.phone_code_hash
            return {"ok": True, "delivery": type(sent.type).__name__, "timeout": getattr(sent, "timeout", None)}

    async def verify_login(self, code: str | None, password: str | None) -> dict[str, Any]:
        if not self.client:
            await self.start()
        if not self.client:
            raise RuntimeError(self.error or "Telegram 客户端未初始化")
        async with self.auth_lock:
            try:
                if password:
                    await self.client.sign_in(password=password)
                elif code and self.auth_phone and self.auth_phone_code_hash:
                    await self.client.sign_in(
                        phone=self.auth_phone,
                        code=code,
                        phone_code_hash=self.auth_phone_code_hash,
                    )
                else:
                    raise ValueError("请先发送验证码")
            except errors.SessionPasswordNeededError:
                return {"ok": False, "password_required": True}
            if not await self.client.is_user_authorized():
                raise RuntimeError("Telegram 授权未完成")
            await self.activate()
            me = await self.client.get_me()
            self.auth_phone = None
            self.auth_phone_code_hash = None
            return {
                "ok": True,
                "password_required": False,
                "account": getattr(me, "username", None) or getattr(me, "first_name", None) or str(me.id),
            }

    async def dialogs(self) -> list[dict[str, Any]]:
        if not self.client or not self.ready:
            raise HTTPException(status_code=503, detail=self.error or "Telegram 尚未连接")
        result: list[dict[str, Any]] = []
        async for dialog in self.client.iter_dialogs(limit=300):
            kind = tg_dialog_kind(dialog)
            if kind is None:
                continue
            entity = dialog.entity
            result.append(
                {
                    "id": dialog.id,
                    "title": dialog.name or "(unnamed)",
                    "kind": kind,
                    "username": getattr(entity, "username", None),
                }
            )
        return result

    async def resolve_username(self, value: str) -> tuple[int, str]:
        if not self.client or not self.ready:
            raise HTTPException(status_code=503, detail="Telegram 尚未连接，无法解析用户名")
        username = value.strip().lstrip("@").strip()
        if not username:
            raise HTTPException(status_code=400, detail="Telegram 用户名不能为空")
        try:
            entity = await self.client.get_entity(username)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"找不到 Telegram 用户名 @{username}") from exc
        if isinstance(entity, User) and entity.bot:
            raise HTTPException(status_code=400, detail="不支持将 Telegram 机器人作为来源")
        if not isinstance(entity, (User, Chat, Channel)):
            raise HTTPException(status_code=400, detail="该 Telegram 用户名不是可监听的用户或群组")
        title = " ".join(filter(None, [getattr(entity, "first_name", None), getattr(entity, "last_name", None)])) or getattr(entity, "title", None) or getattr(entity, "username", None) or username
        return utils.get_peer_id(entity), title

    async def send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        if not self.client or not self.ready:
            raise RuntimeError(self.error or "Telegram 尚未连接")
        await self.client.send_message(chat_id, text, reply_to=reply_to)

    async def add_route(self, row: sqlite3.Row | dict[str, Any]) -> None:
        if not self.client or not self.ready:
            return
        route_id = int(row["id"])
        await self.remove_route(route_id)
        route = Route(int(row["tg_chat_id"]), row["tg_title"], row["qq_type"], int(row["qq_id"]))

        async def forward_handler(event: events.NewMessage.Event) -> None:
            try:
                segments = await build_segments(event)
                result = await send_to_qq(route, self.manager.api_url, self.manager.token, segments)
                qq_message_id = (result.get("data") or {}).get("message_id") if isinstance(result, dict) else None
                if qq_message_id is not None:
                    with db_connect() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO message_links(qq_message_id, route_id, owner_user_id, tg_chat_id, tg_message_id) VALUES (?, ?, ?, ?, ?)",
                            (int(qq_message_id), route_id, self.user_id, route.tg_chat_id, int(event.message.id)),
                        )
                logger.info("Forwarded Telegram message %s via route %s user %s", event.message.id, route_id, self.user_id)
                with db_connect() as db:
                    db.execute("UPDATE routes SET last_error = NULL WHERE id = ?", (route_id,))
            except Exception as exc:
                logger.exception("Forward failed for route %s user %s", route_id, self.user_id)
                with db_connect() as db:
                    db.execute("UPDATE routes SET last_error = ? WHERE id = ?", (str(exc)[:500], route_id))

        self.client.add_event_handler(forward_handler, events.NewMessage(incoming=True, chats=route.tg_chat_id))
        self.handlers[route_id] = forward_handler

    async def remove_route(self, route_id: int) -> None:
        handler = self.handlers.pop(route_id, None)
        if handler and self.client:
            self.client.remove_event_handler(handler)

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        self.client = None
        self.ready = False
        self.handlers.clear()


class MultiUserBridgeRuntime:
    def __init__(self) -> None:
        self.users: dict[int, UserTelegramRuntime] = {}
        self.api_url = os.getenv("NAPCAT_API", "http://127.0.0.1:33100")
        self.token = os.getenv("NAPCAT_ACCESS_TOKEN") or None
        self.lock = asyncio.Lock()

    async def ensure_user(self, user_id: int) -> UserTelegramRuntime:
        async with self.lock:
            runtime = self.users.get(user_id)
            if runtime is None:
                runtime = UserTelegramRuntime(self, user_id)
                self.users[user_id] = runtime
                await runtime.start()
            return runtime

    async def start(self) -> None:
        with db_connect() as db:
            user_ids = [int(row["id"]) for row in db.execute("SELECT id FROM users WHERE active = 1").fetchall()]
        await asyncio.gather(*(self.ensure_user(user_id) for user_id in user_ids))

    async def stop(self) -> None:
        await asyncio.gather(*(item.stop() for item in self.users.values()))
        self.users.clear()

    async def stop_user(self, user_id: int) -> None:
        item = self.users.pop(user_id, None)
        if item:
            await item.stop()

    async def status(self, user_id: int) -> dict[str, Any]:
        item = await self.ensure_user(user_id)
        return {"telegram_ready": item.ready, "telegram_error": item.error}

    async def dialogs(self, user_id: int) -> list[dict[str, Any]]:
        return await (await self.ensure_user(user_id)).dialogs()

    async def send_login_code(self, user_id: int, phone: str) -> dict[str, Any]:
        return await (await self.ensure_user(user_id)).send_login_code(phone)

    async def verify_login(self, user_id: int, code: str | None, password: str | None) -> dict[str, Any]:
        return await (await self.ensure_user(user_id)).verify_login(code, password)

    async def resolve_username(self, user_id: int, username: str) -> tuple[int, str]:
        return await (await self.ensure_user(user_id)).resolve_username(username)

    async def send_message(self, user_id: int, chat_id: int, text: str, reply_to: int | None = None) -> None:
        await (await self.ensure_user(user_id)).send_message(chat_id, text, reply_to=reply_to)

    async def add_route(self, row: sqlite3.Row | dict[str, Any]) -> None:
        await (await self.ensure_user(int(row["owner_user_id"]))).add_route(row)

    async def remove_route(self, route_id: int, user_id: int | None = None) -> None:
        if user_id is not None and user_id in self.users:
            await self.users[user_id].remove_route(route_id)
            return
        for item in self.users.values():
            if route_id in item.handlers:
                await item.remove_route(route_id)
                return


runtime = MultiUserBridgeRuntime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title="TG → QQ Forwarder", lifespan=lifespan)
cors_origins = [item.strip() for item in os.getenv("PANEL_CORS_ORIGINS", "http://localhost:5173").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ready = sum(item.ready for item in runtime.users.values())
    return {"ok": True, "telegram_ready_users": ready, "telegram_users": len(runtime.users)}


@app.post("/api/auth/login")
async def login(payload: LoginPayload, response: Response) -> dict[str, Any]:
    with db_connect() as db:
        row = db.execute("SELECT * FROM users WHERE username = ?", (payload.username,)).fetchone()
    if row is None or not row["active"] or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(int(row["id"])),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return {"user": public_user(row)}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return {"user": user}


@app.get("/api/status")
async def bridge_status(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    telegram = await runtime.status(user["id"])
    with db_connect() as db:
        if user["role"] == "admin":
            total = db.execute("SELECT COUNT(*) AS count FROM routes").fetchone()["count"]
            enabled = db.execute("SELECT COUNT(*) AS count FROM routes WHERE enabled = 1").fetchone()["count"]
        else:
            total = db.execute("SELECT COUNT(*) AS count FROM routes WHERE owner_user_id = ?", (user["id"],)).fetchone()["count"]
            enabled = db.execute("SELECT COUNT(*) AS count FROM routes WHERE owner_user_id = ? AND enabled = 1", (user["id"],)).fetchone()["count"]
    return {**telegram, "routes": total, "enabled_routes": enabled}


@app.get("/api/telegram/dialogs")
async def telegram_dialogs(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    return await runtime.dialogs(user["id"])


@app.post("/api/telegram/auth/send-code")
async def telegram_send_code(payload: TelegramCodePayload, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    try:
        return await runtime.send_login_code(user["id"], payload.phone.strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telegram 验证码发送失败: {exc}") from exc


@app.post("/api/telegram/auth/verify")
async def telegram_verify(payload: TelegramVerifyPayload, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    try:
        return await runtime.verify_login(user["id"], payload.code.strip() if payload.code else None, payload.password)
    except errors.PhoneCodeInvalidError as exc:
        raise HTTPException(status_code=400, detail="验证码错误") from exc
    except errors.PhoneCodeExpiredError as exc:
        raise HTTPException(status_code=400, detail="验证码已过期，请重新发送") from exc
    except errors.PasswordHashInvalidError as exc:
        raise HTTPException(status_code=400, detail="Telegram 二步验证密码错误") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Telegram 授权失败: {exc}") from exc


@app.get("/api/qq/targets")
async def qq_targets(kind: Literal["private", "group"], user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    del user
    action = "get_friend_list" if kind == "private" else "get_group_list"
    try:
        result = await onebot_call(runtime.api_url, runtime.token, action)
    except Exception as exc:
        message = str(exc) or f"无法连接 {runtime.api_url}"
        raise HTTPException(status_code=502, detail=f"NapCat 请求失败（{runtime.api_url}）：{message}") from exc
    items = result.get("data") or []
    if kind == "private":
        return [{"id": item.get("user_id"), "title": item.get("remark") or item.get("nickname") or "(unnamed)"} for item in items]
    return [{"id": item.get("group_id"), "title": item.get("group_name") or "(unnamed)"} for item in items]


def qq_reply_id(payload: dict[str, Any]) -> int | None:
    direct = payload.get("reply_id")
    if direct is None and isinstance(payload.get("reply"), dict):
        direct = payload["reply"].get("id")
    segments = payload.get("message")
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict) and segment.get("type") == "reply":
                direct = (segment.get("data") or {}).get("id", direct)
                break
    try:
        return int(direct) if direct is not None else None
    except (TypeError, ValueError):
        return None


def qq_message_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "image":
            parts.append("[QQ图片]")
        elif kind == "file":
            parts.append(f"[QQ文件: {data.get('name') or 'file'}]")
    return "".join(parts).strip()


@app.post("/api/onebot/events")
async def onebot_event(
    payload: dict[str, Any],
    request: Request,
    access_token: str | None = Query(default=None),
) -> dict[str, Any]:
    expected = runtime.token
    provided = access_token or request.headers.get("Authorization", "")
    if expected:
        # NapCat HTTP clients authenticate the raw JSON body with an HMAC-SHA1
        # signature instead of sending the token as an Authorization header.
        signature = request.headers.get("x-signature", "")
        body = await request.body()
        expected_signature = "sha1=" + hmac.new(expected.encode("utf-8"), body, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(signature, expected_signature) and provided not in {expected, f"Bearer {expected}"}:
            raise HTTPException(status_code=403, detail="OneBot event token invalid")
    if payload.get("post_type") != "message":
        return {"ok": True, "ignored": True}
    reply_id = qq_reply_id(payload)
    text = qq_message_text(payload)
    if reply_id is None or not text:
        return {"ok": True, "ignored": True}
    with db_connect() as db:
        link = db.execute("SELECT * FROM message_links WHERE qq_message_id = ?", (reply_id,)).fetchone()
    if link is None:
        return {"ok": True, "ignored": True}
    try:
        await runtime.send_message(
            int(link["owner_user_id"]),
            int(link["tg_chat_id"]),
            f"[QQ回复] {text}",
            reply_to=int(link["tg_message_id"]),
        )
    except Exception as exc:
        logger.exception("Could not send QQ reply to Telegram")
        with db_connect() as db:
            db.execute("UPDATE routes SET last_error = ? WHERE id = ?", (f"QQ回复失败: {exc}"[:500], link["route_id"]))
        raise HTTPException(status_code=502, detail="Telegram 回复发送失败") from exc
    return {"ok": True, "forwarded": True}


def route_visible(route_id: int, user: dict[str, Any]) -> sqlite3.Row | None:
    with db_connect() as db:
        if user["role"] == "admin":
            return db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        return db.execute("SELECT * FROM routes WHERE id = ? AND owner_user_id = ?", (route_id, user["id"])).fetchone()


@app.get("/api/routes")
async def list_routes(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db_connect() as db:
        if user["role"] == "admin":
            rows = db.execute("SELECT * FROM routes ORDER BY id DESC").fetchall()
        else:
            rows = db.execute("SELECT * FROM routes WHERE owner_user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
    return [public_route(row) for row in rows]


@app.post("/api/routes")
async def create_route(payload: RoutePayload, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    tg_chat_id = payload.tg_chat_id
    tg_title = payload.tg_title.strip()
    if payload.tg_username:
        tg_chat_id, tg_title = await runtime.resolve_username(user["id"], payload.tg_username)
    if tg_chat_id is None:
        raise HTTPException(status_code=400, detail="请选择 Telegram 对话或填写用户名")
    if not tg_title:
        tg_title = str(tg_chat_id)
    with db_connect() as db:
        cursor = db.execute(
            "INSERT INTO routes(owner_user_id, tg_chat_id, tg_title, qq_type, qq_id, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], tg_chat_id, tg_title, payload.qq_type, payload.qq_id, int(payload.enabled)),
        )
        route_id = cursor.lastrowid
        row = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if payload.enabled:
        await runtime.add_route(row)
    return public_route(row)


@app.patch("/api/routes/{route_id}")
async def update_route(route_id: int, payload: RouteUpdate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    row = route_visible(route_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="路由不存在")
    fields: list[str] = []
    values: list[Any] = []
    for name in ("enabled", "qq_type", "qq_id"):
        value = getattr(payload, name)
        if value is not None:
            fields.append(f"{name} = ?")
            values.append(int(value) if name == "enabled" else value)
    if fields:
        values.append(route_id)
        with db_connect() as db:
            db.execute(f"UPDATE routes SET {', '.join(fields)}, last_error = NULL WHERE id = ?", values)
    with db_connect() as db:
        updated = db.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if updated["enabled"]:
        await runtime.add_route(updated)
    else:
        await runtime.remove_route(route_id)
    return public_route(updated)


@app.delete("/api/routes/{route_id}")
async def delete_route(route_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, bool]:
    if route_visible(route_id, user) is None:
        raise HTTPException(status_code=404, detail="路由不存在")
    with db_connect() as db:
        db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
    await runtime.remove_route(route_id)
    return {"ok": True}


@app.get("/api/admin/users")
async def list_users(_: dict[str, Any] = Depends(admin_user)) -> list[dict[str, Any]]:
    with db_connect() as db:
        rows = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [public_user(row) for row in rows]


@app.post("/api/admin/users")
async def create_user(payload: UserPayload, _: dict[str, Any] = Depends(admin_user)) -> dict[str, Any]:
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        with db_connect() as db:
            cursor = db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)",
                (payload.username, password_hash, payload.role),
            )
            row = db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    return public_user(row)


@app.patch("/api/admin/users/{user_id}")
async def update_user(user_id: int, payload: UserUpdate, admin: dict[str, Any] = Depends(admin_user)) -> dict[str, Any]:
    if user_id == admin["id"] and payload.active is False:
        raise HTTPException(status_code=400, detail="不能停用当前管理员账号")
    row = get_user_by_id(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    fields: list[str] = []
    values: list[Any] = []
    if payload.active is not None:
        fields.append("active = ?")
        values.append(int(payload.active))
    if payload.role is not None:
        fields.append("role = ?")
        values.append(payload.role)
    if payload.password is not None:
        try:
            fields.append("password_hash = ?")
            values.append(hash_password(payload.password))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if fields:
        values.append(user_id)
        with db_connect() as db:
            db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    updated = get_user_by_id(user_id)
    if updated["active"]:
        await runtime.ensure_user(user_id)
    else:
        await runtime.stop_user(user_id)
    return public_user(updated)


WEB_DIST = ROOT / "web" / "dist"
if WEB_DIST.exists():
    # Vite emits absolute /assets URLs by default; expose them for both / and /panel/.
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")
    app.mount("/panel", StaticFiles(directory=WEB_DIST, html=True), name="panel")


@app.get("/", include_in_schema=False)
async def root() -> Response:
    panel_index = WEB_DIST / "index.html"
    if panel_index.exists():
        return FileResponse(panel_index)
    return Response("Panel frontend has not been built. Run `npm install && npm run build` in web/.", media_type="text/plain")
