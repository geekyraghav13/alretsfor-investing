"""Cross-platform notification delivery.

The Notifier interface is intentionally narrow (`send(title, body, url=None)`)
so it can be swapped for web push / SSE / webhook delivery when this becomes
a website.

Notifier selection (auto, in `default_notifier()`):
  - If TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set, send Telegram DMs.
  - Else: fall back to native desktop popup.
  - If both desktop + Telegram are wanted, they compose automatically.
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Optional

import requests

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str, url: Optional[str] = None) -> None: ...


class DesktopNotifier(Notifier):
    """Cross-platform desktop popup.

    Strategy: prefer platform-native CLI (most reliable, no extra deps), fall
    back to plyer for anything we don't recognize. The CLI tools are:
      - Linux: `notify-send` (libnotify, present on every modern desktop)
      - macOS: `osascript`
      - Windows: PowerShell Toast notification
    """

    def __init__(self, app_name: str = "Trading Alerts") -> None:
        self.app_name = app_name

    def send(self, title: str, body: str, url: Optional[str] = None) -> None:
        display_body = f"{body}\n{url}" if url else body
        if _native_notify(self.app_name, title, display_body):
            return
        _plyer_fallback(self.app_name, title, display_body)


class LogOnlyNotifier(Notifier):
    def send(self, title: str, body: str, url: Optional[str] = None) -> None:
        log.info("[ALERT] %s | %s%s", title, body, f" | {url}" if url else "")


class TelegramNotifier(Notifier):
    """Send alerts as Telegram bot DMs.

    Setup:
      1. Open Telegram, message @BotFather, send /newbot. Follow prompts.
         BotFather gives you a token like '123456:ABC-XYZ...'.
      2. Start a chat with your new bot (search its name, hit Start).
      3. Get your chat ID: open
         https://api.telegram.org/bot<TOKEN>/getUpdates
         after sending any message to the bot. Look for "chat":{"id": ...}.
      4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.
    """

    API = "https://api.telegram.org"
    MAX_LEN = 4000  # Telegram limit is 4096; leave headroom for formatting.

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def send(self, title: str, body: str, url: Optional[str] = None) -> None:
        text = f"*{_md_escape(title)}*\n{_md_escape(body)}"
        if url:
            text += f"\n{url}"
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN - 3] + "..."
        try:
            r = requests.post(
                f"{self.API}/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if r.status_code != 200:
                log.error("Telegram send failed: %s %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.error("Telegram request error: %s", e)


class CompositeNotifier(Notifier):
    def __init__(self, *children: Notifier) -> None:
        self.children = children

    def send(self, title: str, body: str, url: Optional[str] = None) -> None:
        for child in self.children:
            try:
                child.send(title, body, url)
            except Exception as e:
                log.error("notifier %s failed: %s", type(child).__name__, e)


def _md_escape(text: str) -> str:
    """Escape Telegram Markdown v1 special chars to avoid 400 errors."""
    return text.replace("_", r"\_").replace("*", r"\*").replace("`", r"\`").replace("[", r"\[")


def _native_notify(app_name: str, title: str, body: str) -> bool:
    """Return True if a native CLI notifier dispatched the popup successfully."""
    system = platform.system()
    try:
        if system == "Linux" and shutil.which("notify-send"):
            rc = subprocess.run(
                ["notify-send", "-a", app_name, "-u", "normal", "-t", "15000", title, body],
                check=False,
            ).returncode
            return rc == 0
        if system == "Darwin":
            safe_title = title.replace('"', "'")
            safe_body = body.replace('"', "'")
            rc = subprocess.run(
                ["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}"'],
                check=False,
            ).returncode
            return rc == 0
        if system == "Windows":
            safe_title = title.replace('"', "'")
            safe_body = body.replace('"', "'")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
                f'$t = "{safe_title}"; $b = "{safe_body}"; '
                "$xml = [xml]'<toast><visual><binding template=\"ToastText02\"><text id=\"1\"></text><text id=\"2\"></text></binding></visual></toast>';"
                "$xml.toast.visual.binding.text[0].InnerText = $t;"
                "$xml.toast.visual.binding.text[1].InnerText = $b;"
                "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument; $doc.LoadXml($xml.OuterXml);"
                f"$toast = [Windows.UI.Notifications.ToastNotification]::new($doc);"
                f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_name}').Show($toast)"
            )
            rc = subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False).returncode
            return rc == 0
    except Exception as e:
        log.error("native notification failed: %s", e)
    return False


def _plyer_fallback(app_name: str, title: str, body: str) -> None:
    try:
        from plyer import notification
        notification.notify(title=title, message=body, app_name=app_name, timeout=15)
    except Exception as e:
        log.warning("plyer fallback failed (%s); logging only", e)
        log.info("[ALERT-stdout] %s | %s", title, body)


def default_notifier() -> Notifier:
    children: list[Notifier] = []
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        children.append(TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID))
    has_display = platform.system() in {"Linux", "Darwin", "Windows"} and (
        platform.system() != "Linux" or bool(shutil.which("notify-send"))
    )
    if has_display and not children:
        children.append(DesktopNotifier())
    elif has_display:
        children.append(DesktopNotifier())
    if not children:
        return LogOnlyNotifier()
    if len(children) == 1:
        return children[0]
    return CompositeNotifier(*children)
