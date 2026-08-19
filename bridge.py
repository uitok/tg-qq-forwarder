"""Interactive Telegram-to-QQ bridge using Telethon and OneBot/NapCat."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import User


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
CONFIG_PATH = Path(os.getenv("TG_CONFIG", str(ROOT / "config.json")))
DOWNLOAD_DIR = Path(os.getenv("TG_DOWNLOAD_DIR", str(ROOT / "downloads")))


@dataclass
class Route:
    tg_chat_id: int
    tg_title: str
    qq_type: str
    qq_id: int


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_route() -> Route | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return Route(**data)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {CONFIG_PATH}: {exc}") from exc


def save_route(route: Route) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(asdict(route), ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def tg_dialog_kind(dialog: Any) -> str | None:
    if dialog.is_user:
        entity = dialog.entity
        if isinstance(entity, User) and entity.bot:
            return None
        return "private"
    if dialog.is_group:
        return "group"
    return None


async def choose_telegram_dialog(client: TelegramClient) -> tuple[int, str]:
    choices: list[tuple[int, str, str]] = []
    async for dialog in client.iter_dialogs(limit=300):
        kind = tg_dialog_kind(dialog)
        if kind is None:
            continue
        choices.append((dialog.id, kind, dialog.name or "(unnamed)"))

    if not choices:
        raise RuntimeError("No Telegram private chats or groups were found.")

    print("\nTelegram source chats:")
    for index, (chat_id, kind, title) in enumerate(choices, start=1):
        print(f"  {index:>3}. [{kind:<7}] {title} (id={chat_id})")

    while True:
        raw = input("Select a Telegram source number: ").strip()
        try:
            index = int(raw)
            chat_id, _, title = choices[index - 1]
            return chat_id, title
        except (ValueError, IndexError):
            print("Please enter a valid number.")


async def onebot_call(api_url: str, token: str | None, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=20, headers=headers) as http:
        response = await http.post(
            f"{api_url.rstrip('/')}/{action}",
            json=params or {},
        )
        response.raise_for_status()
        result = response.json()
        if result.get("retcode", 0) != 0:
            raise RuntimeError(f"OneBot {action} failed: {result}")
        return result


async def choose_qq_target(api_url: str, token: str | None) -> tuple[str, int]:
    print("\nQQ target type:")
    print("  1. Friend")
    print("  2. Group")
    while True:
        choice = input("Select 1 or 2: ").strip()
        if choice in {"1", "2"}:
            break
        print("Please enter 1 or 2.")

    action = "get_friend_list" if choice == "1" else "get_group_list"
    try:
        result = await onebot_call(api_url, token, action)
        items = result.get("data") or []
    except Exception as exc:
        print(f"Could not list QQ targets ({exc}).")
        items = []

    if items:
        print("\nQQ targets:")
        for index, item in enumerate(items, start=1):
            if choice == "1":
                title = item.get("remark") or item.get("nickname") or "(unnamed)"
                target_id = item.get("user_id")
            else:
                title = item.get("group_name") or "(unnamed)"
                target_id = item.get("group_id")
            print(f"  {index:>3}. {title} (id={target_id})")
        raw = input("Select a QQ target number, or press Enter to type an ID: ").strip()
        if raw:
            try:
                item = items[int(raw) - 1]
                target_id = item.get("user_id") if choice == "1" else item.get("group_id")
                return ("private" if choice == "1" else "group", int(target_id))
            except (ValueError, IndexError, TypeError):
                print("Invalid selection; falling back to manual ID input.")

    raw = input("Enter the QQ user/group ID: ").strip()
    try:
        return ("private" if choice == "1" else "group", int(raw))
    except ValueError as exc:
        raise RuntimeError("QQ target ID must be a number.") from exc


def text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


async def telegram_sender_name(event: events.NewMessage.Event) -> str:
    sender = await event.get_sender()
    if sender is None:
        return "Telegram"
    name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
    return name or getattr(sender, "title", None) or getattr(sender, "username", None) or "Telegram"


async def build_segments(event: events.NewMessage.Event) -> list[dict[str, Any]]:
    sender = await telegram_sender_name(event)
    prefix = f"[TG] {sender}:\n"
    segments: list[dict[str, Any]] = []

    if event.raw_text:
        segments.append(text_segment(prefix + event.raw_text))

    if event.message.photo:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = await event.message.download_media(file=DOWNLOAD_DIR)
        if path:
            encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            if not event.raw_text:
                segments.append(text_segment(prefix.rstrip()))
            segments.append({"type": "image", "data": {"file": f"base64://{encoded}"}})
            Path(path).unlink(missing_ok=True)
            return segments

    if segments:
        return segments

    media_name = type(event.message.media).__name__ if event.message.media else "media"
    segments.append(text_segment(f"{prefix}[Unsupported Telegram media: {media_name}]"))
    return segments


async def send_to_qq(route: Route, api_url: str, token: str | None, segments: list[dict[str, Any]]) -> dict[str, Any]:
    action = "send_private_msg" if route.qq_type == "private" else "send_group_msg"
    key = "user_id" if route.qq_type == "private" else "group_id"
    return await onebot_call(
        api_url,
        token,
        action,
        {key: route.qq_id, "message": segments},
    )


async def run() -> None:
    load_dotenv(ROOT / ".env")
    api_id = int(require_env("TG_API_ID"))
    api_hash = require_env("TG_API_HASH")
    api_url = require_env("NAPCAT_API")
    token = os.getenv("NAPCAT_ACCESS_TOKEN")
    session_name = os.getenv("TG_SESSION", str(ROOT / "telegram_userbot"))

    client = TelegramClient(session_name, api_id, api_hash)
    await client.start()

    route = load_route()
    if route is None:
        chat_id, title = await choose_telegram_dialog(client)
        qq_type, qq_id = await choose_qq_target(api_url, token)
        route = Route(chat_id, title, qq_type, qq_id)
        save_route(route)
        print(f"Saved route to {CONFIG_PATH}")
    else:
        print(f"Loaded route: Telegram {route.tg_title} -> QQ {route.qq_type} {route.qq_id}")

    @client.on(events.NewMessage(incoming=True, chats=route.tg_chat_id))
    async def forward_handler(event: events.NewMessage.Event) -> None:
        try:
            segments = await build_segments(event)
            await send_to_qq(route, api_url, token, segments)
            print(f"Forwarded Telegram message {event.message.id}")
        except Exception as exc:
            print(f"Forward failed for Telegram message {event.message.id}: {exc}", file=sys.stderr)

    print("Bridge is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
