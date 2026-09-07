"""Telegram YES/NO approval gate for KD Job Hunter.

No credentials are stored in git. The bot token and chat ID are loaded from
environment variables named in profile.yaml.
"""

import hashlib
import os
from typing import Optional

import httpx


class TelegramApproval:
    def __init__(self, profile: dict):
        cfg = profile.get("approval", {})
        tg = cfg.get("telegram", {})
        self.enabled = bool(cfg.get("enabled", False))
        self.timeout_seconds = int(cfg.get("decision_timeout_seconds", 43200))
        token_env = tg.get("bot_token_env", "KD_JOB_HUNTER_TELEGRAM_BOT_TOKEN")
        chat_env = tg.get("chat_id_env", "KD_JOB_HUNTER_TELEGRAM_CHAT_ID")
        self.bot_token = os.getenv(token_env, "")
        self.chat_id = os.getenv(chat_env, "")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token and self.chat_id)

    def require_configured(self) -> None:
        if not self.enabled:
            raise RuntimeError("Telegram approvals are disabled in profile.yaml")
        if not self.bot_token or not self.chat_id:
            raise RuntimeError(
                "Telegram approval is enabled but credentials are missing. "
                "Set KD_JOB_HUNTER_TELEGRAM_BOT_TOKEN and "
                "KD_JOB_HUNTER_TELEGRAM_CHAT_ID in the runtime environment."
            )

    @staticmethod
    def nonce_for(job_id: str) -> str:
        return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]

    async def send_status(self, text: str) -> int:
        """Send a plain Telegram status/summary message. Returns message_id."""
        self.require_configured()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram sendMessage failed: {data}")
            return int(data["result"]["message_id"])

    async def send_job_for_approval(
        self,
        job: dict,
        tailored_resume_path: str = "",
        tailored_summary: str = "",
    ) -> tuple[str, int]:
        """Send one job with YES/NO buttons. Returns (nonce, message_id)."""
        self.require_configured()
        nonce = self.nonce_for(job["id"])
        score = job.get("match_score") or 0
        reasoning = (job.get("reasoning") or "").strip()
        if len(reasoning) > 900:
            reasoning = reasoning[:897] + "..."

        lines = [
            "KD JOB HUNTER — APPROVAL REQUIRED",
            "",
            f"{job.get('title', 'Unknown role')} @ {job.get('company', 'Unknown company')}",
            f"Match: {score}/100",
            f"Location: {job.get('location') or 'Not listed'}",
        ]
        if reasoning:
            lines += ["", f"Why it fits: {reasoning}"]
        if tailored_summary:
            summary = tailored_summary.strip()
            if len(summary) > 700:
                summary = summary[:697] + "..."
            lines += ["", f"Tailored positioning: {summary}"]
        if tailored_resume_path:
            lines += ["", "Tailored resume: ready"]
        if job.get("apply_url"):
            lines += ["", f"Apply URL: {job['apply_url']}"]
        lines += ["", "Submit this application?"]

        keyboard = {
            "inline_keyboard": [[
                {"text": "YES — Submit", "callback_data": f"kdj:yes:{nonce}"},
                {"text": "NO — Skip", "callback_data": f"kdj:no:{nonce}"},
            ]]
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": "\n".join(lines),
                    "reply_markup": keyboard,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram sendMessage failed: {data}")
            return nonce, int(data["result"]["message_id"])

    async def poll_decisions(self, timeout: int = 2) -> list[dict]:
        """Return callback decisions currently waiting in Telegram updates."""
        self.require_configured()
        async with httpx.AsyncClient(timeout=max(timeout + 5, 10)) as client:
            response = await client.get(
                f"{self.base_url}/getUpdates",
                params={
                    "timeout": timeout,
                    "allowed_updates": '["callback_query"]',
                },
            )
            response.raise_for_status()
            payload = response.json()
            decisions = []
            max_update_id: Optional[int] = None

            for update in payload.get("result", []):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    max_update_id = max(update_id, max_update_id or update_id)
                callback = update.get("callback_query") or {}
                data = callback.get("data", "")
                if not data.startswith("kdj:"):
                    continue
                parts = data.split(":", 2)
                if len(parts) != 3 or parts[1] not in {"yes", "no"}:
                    continue
                message = callback.get("message") or {}
                chat = message.get("chat") or {}
                if str(chat.get("id")) != str(self.chat_id):
                    continue
                decisions.append({
                    "nonce": parts[2],
                    "approved": parts[1] == "yes",
                    "callback_query_id": callback.get("id", ""),
                    "message_id": message.get("message_id"),
                })

            # Advance the bot update cursor so old button presses are not replayed.
            if max_update_id is not None:
                await client.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": max_update_id + 1, "timeout": 0},
                )

            return decisions

    async def acknowledge(self, callback_query_id: str, text: str) -> None:
        if not callback_query_id:
            return
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                await client.post(
                    f"{self.base_url}/answerCallbackQuery",
                    json={"callback_query_id": callback_query_id, "text": text},
                )
            except Exception:
                pass
