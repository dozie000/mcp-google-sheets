"""send_push: deliver a phone notification through ntfy.

Registers one MCP tool so scheduled Claude runs (which can reach this
connector but not arbitrary websites) can send a push that opens a link.

Environment:
    NTFY_TOPIC     secret topic name the phone app is subscribed to.
                   Unset => the tool is not registered.
    NTFY_SERVER    default https://ntfy.sh
    DECIDE_TOKEN   shared secret; callers must pass it as `key`.
"""

import hmac
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict

logger = logging.getLogger(__name__)

MAX_TITLE = 120
MAX_MESSAGE = 1500


def _ascii(text: str) -> str:
    """HTTP header values must be latin-1; keep titles plain ASCII."""
    return (text.replace("—", "-").replace("–", "-").replace("·", "|")
            .encode("ascii", "ignore").decode("ascii"))


def send_push_impl(key: str, title: str, message: str,
                   click_url: str = "", priority: int = 3) -> Dict[str, Any]:
    expected = os.environ.get("DECIDE_TOKEN", "")
    topic = os.environ.get("NTFY_TOPIC", "")
    if not expected or not topic:
        return {"ok": False, "error": "push not configured"}
    if not hmac.compare_digest(expected, key or ""):
        return {"ok": False, "error": "bad key"}
    if not message or len(message) > MAX_MESSAGE:
        return {"ok": False, "error": "message missing or too long"}
    if click_url and not click_url.startswith("https://"):
        return {"ok": False, "error": "click_url must be https"}
    priority = priority if priority in (1, 2, 3, 4, 5) else 3

    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": _ascii(title or "Coach")[:MAX_TITLE], "Priority": str(priority)}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(f"{server}/{topic}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"ok": 200 <= resp.status < 300, "status": resp.status}
    except urllib.error.HTTPError as e:
        logger.error("ntfy HTTP %s", e.code)
        return {"ok": False, "error": f"ntfy returned {e.code}"}
    except Exception as e:
        logger.error("ntfy failed: %s", type(e).__name__)
        return {"ok": False, "error": "could not reach ntfy"}


def register(mcp) -> None:
    if not os.environ.get("NTFY_TOPIC") or not os.environ.get("DECIDE_TOKEN"):
        logger.info("notify: NTFY_TOPIC/DECIDE_TOKEN not set; send_push disabled")
        return

    @mcp.tool()
    def send_push(key: str, title: str, message: str,
                  click_url: str = "", priority: int = 3) -> Dict[str, Any]:
        """Send a push notification to the owner's phone.

        Args:
            key: shared secret (required).
            title: short ASCII title.
            message: notification body (plain text).
            click_url: optional https URL opened when the notification is tapped.
            priority: 1 (min) to 5 (urgent); default 3.
        """
        return send_push_impl(key, title, message, click_url, priority)

    logger.info("notify: send_push enabled")
