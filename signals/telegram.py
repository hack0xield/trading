"""Telegram delivery over the Bot API.

Standard library only — one HTTPS POST per chat. Adding a dependency for that
would be the tail wagging the dog, and this has to run in whatever sandbox the
scheduler gives it.

`dry_run` is the default in the CLI on purpose: a signal system that can send
before you have read what it would send is a way to message strangers by
accident.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096  # Telegram's hard limit on a single message


@dataclass(slots=True)
class Delivery:
    chat_id: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class Notifier:
    token: str | None = None
    dry_run: bool = True
    timeout: int = 20
    sent: list[Delivery] = field(default_factory=list)

    def send(self, chat_ids: list[str], text: str) -> list[Delivery]:
        """Post `text` to each chat. Never raises — a dead chat must not stop the rest."""
        if len(text) > MAX_LEN:
            text = text[: MAX_LEN - 40].rstrip() + "\n… (truncated)"

        out: list[Delivery] = []
        for chat_id in chat_ids:
            if self.dry_run:
                out.append(Delivery(chat_id, True, "dry-run, not sent"))
                continue
            if not self.token:
                out.append(Delivery(chat_id, False, "no bot token configured"))
                continue
            out.append(self._post(chat_id, text))
        self.sent.extend(out)
        return out

    def _post(self, chat_id: str, text: str) -> Delivery:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        request = urllib.request.Request(
            API.format(token=self.token, method="sendMessage"),
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
            if body.get("ok"):
                return Delivery(chat_id, True, f"message_id={body['result'].get('message_id')}")
            return Delivery(chat_id, False, str(body.get("description", "unknown error")))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            try:
                detail = json.loads(detail).get("description", detail)
            except ValueError:
                pass
            return Delivery(chat_id, False, f"HTTP {exc.code}: {detail}")
        except Exception as exc:  # network down, DNS, TLS
            return Delivery(chat_id, False, f"{type(exc).__name__}: {exc}")

    def check(self) -> tuple[bool, str]:
        """Verify the token with getMe, so setup fails loudly and early."""
        if not self.token:
            return False, "no bot token configured"
        try:
            with urllib.request.urlopen(
                API.format(token=self.token, method="getMe"), timeout=self.timeout
            ) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
            if body.get("ok"):
                return True, f"@{body['result'].get('username')}"
            return False, str(body.get("description"))
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def discover_chats(token: str, timeout: int = 20) -> tuple[list[dict], str]:
    """Every chat the bot has heard from, via getUpdates.

    A bot cannot enumerate its chats and cannot message one that has never
    contacted it, so the only way to learn a chat id is to have someone speak
    to the bot first. That asymmetry is Telegram's anti-spam design, and it is
    why this returns nothing until you send the bot a message.
    """
    url = API.format(token=token, method="getUpdates")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not body.get("ok"):
        return [], str(body.get("description", "unknown error"))

    seen: dict[str, dict] = {}
    for update in body.get("result", []):
        # A chat id can arrive on any of these, depending on what happened.
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if not chat:
                continue
            seen[str(chat["id"])] = {
                "id": str(chat["id"]),
                "type": chat.get("type", "?"),
                "title": chat.get("title")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or chat.get("username", ""),
            }
    return list(seen.values()), ""


def escape(text: str) -> str:
    """Escape for Telegram's HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
