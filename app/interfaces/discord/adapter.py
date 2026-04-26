"""Discord interface adapter.

Authentication:
  Bot token stored encrypted in interfaces.config_json under the key
  'bot_token'.

Polling:
  Uses the Discord REST API to poll configured channel IDs. A per-channel
  last-seen message watermark is stored in config_json under
  'last_seen_message_ids' so the first poll can establish a baseline without
  importing existing channel history.

Routing:
  Each Discord channel or thread maps to one KoreComms conversation via its
  channel ID. Replies are posted back into the same channel/thread.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from app import crypto, database as db
from app.interfaces.common.base import BaseInterface

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"
_SECRET_KEYS = ("bot_token",)
_MESSAGE_LIMIT = 100
_DISCORD_MESSAGE_MAX = 2000


class DiscordInterface(BaseInterface):

    def _decrypt_config(self) -> dict:
        raw = json.loads(self.config.get("config_json", "{}"))
        result = {}
        for key, value in raw.items():
            if key in _SECRET_KEYS and value:
                try:
                    result[key] = crypto.decrypt(value)
                except Exception:
                    result[key] = value
            else:
                result[key] = value
        return result

    @staticmethod
    def _normalize_channel_ids(value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            parts = value.replace(",", "\n").splitlines()
            return [part.strip() for part in parts if part.strip()]
        return []

    @staticmethod
    def _split_content(content: str) -> list[str]:
        text = content.strip()
        if not text:
            return [""]
        parts: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= _DISCORD_MESSAGE_MAX:
                parts.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, _DISCORD_MESSAGE_MAX)
            if split_at <= 0:
                split_at = remaining.rfind(" ", 0, _DISCORD_MESSAGE_MAX)
            if split_at <= 0:
                split_at = _DISCORD_MESSAGE_MAX
            parts.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        return parts or [""]

    @staticmethod
    def _sender_name(message: dict) -> str:
        author = message.get("author") or {}
        return (
            author.get("global_name")
            or author.get("username")
            or author.get("id")
            or "Discord"
        )

    @staticmethod
    def _message_content(message: dict) -> str:
        parts: list[str] = []
        body = (message.get("content") or "").strip()
        if body:
            parts.append(body)
        attachment_urls = [
            attachment.get("url", "").strip()
            for attachment in message.get("attachments", [])
            if attachment.get("url")
        ]
        if attachment_urls:
            parts.append("\n".join(attachment_urls))
        return "\n\n".join(part for part in parts if part).strip()

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list:
        cfg = self._decrypt_config()
        token = cfg.get("bot_token", "")
        if not token:
            raise RuntimeError("Discord bot token is not configured")

        url = f"{_API_BASE}{path}"
        body = None
        headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "KoreComms/1.0",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Discord API {method} {path} failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Discord API {method} {path} failed: {exc.reason}") from exc

        if not raw:
            return {}
        return json.loads(raw)

    def _send_message(self, channel_id: str, content: str, reference_message_id: str | None = None) -> dict:
        payload = {
            "content": content,
            "allowed_mentions": {"parse": []},
        }
        if reference_message_id:
            payload["message_reference"] = {"message_id": reference_message_id}
        result = self._request("POST", f"/channels/{channel_id}/messages", payload)
        if not isinstance(result, dict):
            raise RuntimeError("Discord API returned an unexpected response while sending a message")
        return result

    def poll(self) -> list[dict]:
        cfg = self._decrypt_config()
        channel_ids = self._normalize_channel_ids(cfg.get("channel_ids", []))
        if not cfg.get("bot_token"):
            logger.warning("Discord interface %d: no bot token, skipping poll", self.interface_id)
            return []
        if not channel_ids:
            logger.warning("Discord interface %d: no channel_ids configured, skipping poll", self.interface_id)
            return []

        last_seen_message_ids = cfg.get("last_seen_message_ids") or {}
        if not isinstance(last_seen_message_ids, dict):
            last_seen_message_ids = {}

        updated_watermarks = dict(last_seen_message_ids)
        changed = False
        messages: list[dict] = []

        for channel_id in channel_ids:
            after_id = str(last_seen_message_ids.get(channel_id, "")).strip()
            if not after_id:
                result = self._request("GET", f"/channels/{channel_id}/messages?limit=1")
                if isinstance(result, list) and result:
                    updated_watermarks[channel_id] = result[0].get("id", "")
                    changed = True
                continue

            query = urllib.parse.urlencode({"after": after_id, "limit": _MESSAGE_LIMIT})
            result = self._request("GET", f"/channels/{channel_id}/messages?{query}")
            if not isinstance(result, list) or not result:
                continue

            chronological = list(reversed(result))
            for item in chronological:
                author = item.get("author") or {}
                if author.get("bot"):
                    continue
                content = self._message_content(item)
                if not content:
                    continue
                message_id = str(item.get("id", "")).strip()
                target_channel_id = str(item.get("channel_id", channel_id)).strip()
                if not message_id or not target_channel_id:
                    continue
                messages.append(
                    {
                        "external_message_id": message_id,
                        "external_thread_id": target_channel_id,
                        "sender": self._sender_name(item),
                        "subject": None,
                        "content": content,
                        "channel_type": "discord",
                    }
                )

            newest_id = str(result[0].get("id", "")).strip()
            if newest_id and updated_watermarks.get(channel_id) != newest_id:
                updated_watermarks[channel_id] = newest_id
                changed = True

        if changed:
            raw_cfg = json.loads(self.config.get("config_json", "{}"))
            raw_cfg["last_seen_message_ids"] = updated_watermarks
            db.interface_update(
                self.interface_id,
                self.config.get("name", self.name),
                raw_cfg,
                bool(self.config.get("enabled", True)),
            )

        return messages

    def route_reply(self, conversation_id: int, content: str) -> None:
        conv = db.conversation_get(conversation_id)
        if conv is None:
            raise RuntimeError(f"Conversation {conversation_id} not found")

        channel_id = (conv.get("external_thread_id") or "").strip()
        if not channel_id:
            raise RuntimeError(f"Conversation {conversation_id} has no Discord channel binding")

        last_inbound = db.external_message_get_last_inbound(conversation_id)
        reference_message_id = None
        if last_inbound:
            candidate = (last_inbound.get("external_message_id") or "").strip()
            if candidate and not candidate.startswith("kc:"):
                reference_message_id = candidate

        for index, chunk in enumerate(self._split_content(content)):
            self._send_message(
                channel_id,
                chunk,
                reference_message_id=reference_message_id if index == 0 else None,
            )

    def send_new(self, recipient: str, subject: str, content: str) -> dict:
        channel_id = recipient.strip()
        if not channel_id:
            raise RuntimeError("Discord recipient must be a channel or thread ID")

        outbound = content.strip()
        if subject.strip():
            outbound = f"**{subject.strip()}**\n\n{outbound}" if outbound else f"**{subject.strip()}**"

        sent_messages: list[dict] = []
        for chunk in self._split_content(outbound):
            sent_messages.append(self._send_message(channel_id, chunk))

        if not sent_messages:
            raise RuntimeError("Discord send_new did not send any message")

        first_message = sent_messages[0]
        return {
            "external_thread_id": channel_id,
            "external_message_id": first_message.get("id", channel_id),
        }