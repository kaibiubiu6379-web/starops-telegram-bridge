"""Receive STAROps webhook notifications and forward the complete payload to Telegram."""

from __future__ import annotations

import hmac
import json
import logging
from logging.handlers import WatchedFileHandler
import os
import re
import time
from typing import Any

import requests
from flask import Flask, jsonify, request

MAX_BODY_BYTES = 10 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 3900
TELEGRAM_TIMEOUT_SECONDS = 4
SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    "accesskey",
    "privatekey",
    "cookie",
    "apikey",
)
SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(authorization|access[_-]?key|secret|password|passwd|token)(\s*[:=]\s*)([^\s,;]+)"
)

logger = logging.getLogger("starops_telegram_bridge")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger.propagate = False

log_path = os.getenv("LOG_FILE")
if log_path:
    log_handler = WatchedFileHandler(log_path, encoding="utf-8")
else:
    log_handler = logging.StreamHandler()
log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(log_handler)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES


def _read_report() -> tuple[str, str]:
    """Return a human-readable report and a suitable file extension."""
    raw = request.get_data(cache=True)
    if not raw:
        return "(empty webhook payload)", "txt"

    if request.is_json:
        try:
            payload: Any = json.loads(raw)
            return json.dumps(payload, ensure_ascii=False, indent=2), "json"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    if request.form:
        payload = request.form.to_dict(flat=False)
        return json.dumps(payload, ensure_ascii=False, indent=2), "json"

    return raw.decode("utf-8", errors="replace"), "txt"


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_log_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_TEXT_PATTERN.sub(r"\1\2[REDACTED]", value)
    return value


def _safe_request_headers() -> dict[str, str]:
    safe_headers = {}
    for name, value in request.headers.items():
        normalized_name = re.sub(r"[^a-z0-9]", "", name.lower())
        if any(part in normalized_name for part in SENSITIVE_KEY_PARTS):
            safe_headers[name] = "[REDACTED]"
        else:
            safe_headers[name] = value
    return safe_headers


def _log_webhook_payload() -> None:
    raw = request.get_data(cache=True)
    if request.is_json:
        try:
            payload: Any = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = raw.decode("utf-8", errors="replace")
    elif request.form:
        payload = request.form.to_dict(flat=False)
    else:
        payload = raw.decode("utf-8", errors="replace")

    logger.info(
        "Webhook payload from %s: %s",
        request.remote_addr or "unknown",
        json.dumps(_redact_log_value(payload), ensure_ascii=False, separators=(",", ":")),
    )


def _telegram_config() -> tuple[str, str] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat_id) if token and chat_id else None


def _webhook_auth_enabled() -> bool:
    return os.getenv("WEBHOOK_AUTH_ENABLED", "true").strip().lower() not in {
        "false",
        "0",
        "no",
        "off",
    }


def _send_to_telegram(report: str, extension: str) -> None:
    config = _telegram_config()
    if config is None:
        raise RuntimeError("Telegram is not configured")

    token, chat_id = config
    api = f"https://api.telegram.org/bot{token}"
    if len(report) <= TELEGRAM_TEXT_LIMIT:
        response = requests.post(
            f"{api}/sendMessage",
            json={"chat_id": chat_id, "text": report, "disable_web_page_preview": True},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
    else:
        files = {
            "document": (
                f"starops-report.{extension}",
                report.encode("utf-8"),
                "application/json" if extension == "json" else "text/plain; charset=utf-8",
            )
        }
        response = requests.post(
            f"{api}/sendDocument",
            data={"chat_id": chat_id, "caption": "STAROps 完整报告（原始 Webhook 内容）"},
            files=files,
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )

    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected the report")


@app.get("/healthz")
def healthz():
    return jsonify(status="ok", telegram_configured=_telegram_config() is not None)


@app.before_request
def start_request_timer():
    request.environ["starops_request_started_at"] = time.perf_counter()


@app.after_request
def log_http_request(response):
    started_at = request.environ.get("starops_request_started_at")
    elapsed_ms = (time.perf_counter() - started_at) * 1000 if started_at else 0
    expected_secret = os.getenv("WEBHOOK_SHARED_SECRET", "")
    provided_secret = request.headers.get("X-Webhook-Token", "")
    auth_enabled = _webhook_auth_enabled()
    token_matches = bool(
        auth_enabled
        and expected_secret
        and provided_secret
        and hmac.compare_digest(provided_secret, expected_secret)
    )
    request_details = {
        "method": request.method,
        "path": request.path,
        "status": response.status_code,
        "client_ip": request.remote_addr or "unknown",
        "elapsed_ms": round(elapsed_ms, 1),
        "content_type": request.content_type,
        "content_length": request.content_length,
        "headers": _safe_request_headers(),
        "webhook_auth_enabled": auth_enabled,
        "webhook_token_present": bool(provided_secret),
        "webhook_token_matches": token_matches if auth_enabled else None,
        "x_forwarded_for": request.headers.get("X-Forwarded-For"),
    }
    logger.info(
        "HTTP request: %s",
        json.dumps(request_details, ensure_ascii=False, separators=(",", ":")),
    )
    return response


@app.post("/webhook")
def webhook():
    shared_secret = os.getenv("WEBHOOK_SHARED_SECRET", "")
    provided_secret = request.headers.get("X-Webhook-Token", "")
    if _webhook_auth_enabled() and (
        not shared_secret or not hmac.compare_digest(provided_secret, shared_secret)
    ):
        return jsonify(error="unauthorized"), 401

    if _telegram_config() is None:
        return jsonify(error="telegram_not_configured"), 503

    _log_webhook_payload()
    report, extension = _read_report()
    try:
        _send_to_telegram(report, extension)
    except requests.RequestException as exc:
        # Do not log exception text: the Telegram URL contains the bot token.
        logger.error("Telegram delivery failed (%s; details omitted)", type(exc).__name__)
        return jsonify(error="telegram_delivery_failed"), 502
    except (RuntimeError, ValueError) as exc:
        logger.error("Telegram delivery failed (%s; details omitted)", type(exc).__name__)
        return jsonify(error="telegram_delivery_failed"), 502

    logger.info("Forwarded STAROps report (%d characters)", len(report))
    return jsonify(status="forwarded"), 200


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(error="payload_too_large", max_bytes=MAX_BODY_BYTES), 413
