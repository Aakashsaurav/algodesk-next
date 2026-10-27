"""Configuration schema for Telegram notifier plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TelegramNotifierConfig(BaseModel):
    """Parameters for the Telegram notifier plugin."""

    model_config = ConfigDict(extra="ignore")

    bot_token: str = Field(default="secret:telegram_bot_token")
    chat_id: str = Field(default="secret:telegram_chat_id")
    parse_mode: str = Field(default="HTML")
