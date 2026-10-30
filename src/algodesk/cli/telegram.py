"""Telegram notifier setup and diagnostic test CLI commands."""

from __future__ import annotations

import getpass
import time

import httpx
import typer

from algodesk.api.errors import ConfigError
from algodesk.cli.common import _open_rt, handle_cli_errors
from algodesk.cli.secrets import _resolve_secrets_provider

telegram_app = typer.Typer(help="Telegram bot configuration and diagnostic commands.")


@telegram_app.command("setup")
@handle_cli_errors
def telegram_setup_cmd(
    ctx: typer.Context,
    token: str | None = typer.Option(None, "--token", help="Bot API token (prompts if omitted)"),
    chat_id: str | None = typer.Option(None, "--chat-id", help="Chat ID (polls if omitted)"),
    timeout_seconds: float = typer.Option(30.0, "--timeout", help="Polling timeout in seconds"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
) -> None:
    """Interactively configure Telegram bot token and chat ID in secrets."""
    if token is not None:
        bot_token = token.strip()
        if not bot_token:
            raise typer.BadParameter("Bot token cannot be empty")
    else:
        bot_token = getpass.getpass("Enter Telegram Bot Token: ").strip()
        if not bot_token:
            raise typer.BadParameter("Bot token cannot be empty")

    req_timeout: float | None = None
    with _open_rt(ctx, ensure=False) as rt:
        if rt.params is not None:
            try:
                req_timeout = float(rt.params.get("telegram.timeout_seconds"))
            except Exception:
                pass

    resolved_chat_id = chat_id.strip() if chat_id is not None else None
    if not resolved_chat_id:
        typer.echo("Polling for /start message from your Telegram client...")
        typer.echo("Please open your bot in Telegram and send '/start'.")
        start_time = time.time()
        poll_url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

        while time.time() - start_time < timeout_seconds:
            try:
                with httpx.Client(timeout=req_timeout) as client:
                    resp = client.get(poll_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        for update in data.get("result", []):
                            msg = update.get("message", {})
                            text = msg.get("text", "")
                            if "/start" in text:
                                resolved_chat_id = str(msg.get("chat", {}).get("id", ""))
                                break
            except Exception:
                pass

            if resolved_chat_id:
                break
            time.sleep(1.0)

        if not resolved_chat_id:
            raise ConfigError(
                what=f"Timed out after {int(timeout_seconds)}s waiting for /start message",
                where="cli.telegram.setup",
                fix="Send /start to your bot in Telegram and re-run setup or use --chat-id",
            )

    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, run=run)
        prov.set("telegram_bot_token", bot_token)
        prov.set("telegram_chat_id", resolved_chat_id)

    # Send test confirmation
    send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": resolved_chat_id,
        "text": "✅ AlgoDesk Next Telegram notifications enabled!",
        "parse_mode": "HTML",
    }
    try:
        with httpx.Client(timeout=req_timeout) as client:
            client.post(send_url, json=payload)
    except Exception as exc:
        typer.echo(f"Warning: Failed to send confirmation message: {exc}")

    typer.echo(f"OK: configured bot token and chat_id {resolved_chat_id}")


@telegram_app.command("test")
@handle_cli_errors
def telegram_test_cmd(
    ctx: typer.Context,
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
) -> None:
    """Send a diagnostic test message using configured Telegram credentials."""
    req_timeout: float | None = None
    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, run=run)
        if rt.params is not None:
            try:
                req_timeout = float(rt.params.get("telegram.timeout_seconds"))
            except Exception:
                pass
        try:
            bot_token = prov.get("telegram_bot_token")
            target_chat_id = prov.get("telegram_chat_id")
        except Exception as exc:
            raise ConfigError(
                what=f"Could not load telegram secrets: {exc}",
                where="cli.telegram.test",
                fix="Run 'algodesk telegram setup' first",
            ) from exc

    bot_token = bot_token.strip() if bot_token else ""
    target_chat_id = target_chat_id.strip() if target_chat_id else ""
    if not bot_token or not target_chat_id:
        raise ConfigError(
            what="Telegram bot token or chat ID not found in secrets",
            where="cli.telegram.test",
            fix="Run 'algodesk telegram setup' to configure credentials",
        )

    send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": "🔔 AlgoDesk Next diagnostic test message.",
        "parse_mode": "HTML",
    }
    try:
        with httpx.Client(timeout=req_timeout) as client:
            resp = client.post(send_url, json=payload)
            if resp.status_code != 200:
                raise ConfigError(
                    what=f"Telegram API error HTTP {resp.status_code}: {resp.text}",
                    where="cli.telegram.test",
                    fix="Verify bot token and chat ID via 'algodesk telegram setup'",
                )
    except Exception as exc:
        raise ConfigError(
            what=f"Failed to send test message: {exc}",
            where="cli.telegram.test",
            fix="Check network connectivity and bot token",
        ) from exc

    typer.echo(f"OK: sent test message to chat {target_chat_id}")
