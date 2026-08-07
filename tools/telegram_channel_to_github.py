#!/usr/bin/env python3
"""Archive safe Telegram channel text posts as Markdown files in GitHub.

This tool intentionally uses long polling rather than changing a bot webhook.
It only publishes text-only `channel_post` updates from one configured channel.
Messages with media, likely credentials, or likely personal data are skipped.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


TELEGRAM_API = "https://api.telegram.org"
GITHUB_API = "https://api.github.com"
MAX_MESSAGE_CHARS = 12_000
RETRY_COUNT = 3
RETRY_DELAY_SECONDS = 1.5
MEDIA_FIELDS = frozenset(
    {
        "animation",
        "audio",
        "document",
        "photo",
        "sticker",
        "video",
        "video_note",
        "voice",
    }
)

# These are conservative publication blockers. They are not a replacement for
# human moderation, but prevent common accidental credential/PII disclosures.
RISK_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
    "telegram_bot_token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "credential_assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*['\"]?[^\s,'\"]{12,}"
    ),
    "email_address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "phone_number": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    "china_id_number": re.compile(r"(?<!\d)\d{17}[\dXx](?![\dA-Za-z])"),
}


class ApiError(RuntimeError):
    """A remote API call failed without a safe retry path."""


@dataclass(frozen=True)
class Config:
    bot_token: str | None
    github_token: str | None
    channel_id: int
    repository: str
    branch: str
    path_prefix: str
    state_file: Path
    github_state_path: str | None
    poll_timeout: int
    once: bool
    dry_run: bool
    fixture: Path | None


@dataclass
class Summary:
    created: int = 0
    already_exists: int = 0
    skipped_risky: int = 0
    skipped_media: int = 0
    ignored: int = 0


shutdown_requested = False


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def handle_shutdown(signum: int, _frame: Any) -> None:
    global shutdown_requested
    shutdown_requested = True
    logging.info("received signal %s; stopping after the current request", signum)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor a Telegram channel and archive safe text posts in GitHub.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--channel-id", required=True, type=int, help="Telegram channel numeric ID, usually starts with -100")
    parser.add_argument("--repo", default="hwxc129/soulai-static-client", help="GitHub repository as OWNER/REPO")
    parser.add_argument("--branch", default="main", help="Target Git branch")
    parser.add_argument("--path-prefix", default="telegram-posts", help="Directory for generated Markdown files")
    parser.add_argument("--state-file", default=".runtime/telegram-github-state.json", help="Local idempotency state file")
    parser.add_argument("--github-state-path", help="Optional repository file for persistent update state; required for stateless runners")
    parser.add_argument("--poll-timeout", type=int, default=45, choices=range(1, 51), metavar="SECONDS", help="Telegram long-poll timeout")
    parser.add_argument("--once", action="store_true", help="Process one Telegram response, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Print planned GitHub writes without sending requests or writing state")
    parser.add_argument("--fixture", type=Path, help="Read a saved getUpdates JSON response instead of calling Telegram; use with --dry-run")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging; never logs credentials or message bodies")
    return parser.parse_args()


def make_config(args: argparse.Namespace) -> Config:
    if "/" not in args.repo or args.repo.count("/") != 1:
        raise ValueError("--repo must use OWNER/REPO form")
    prefix = args.path_prefix.strip("/")
    if not prefix or any(part in {"", ".", ".."} for part in prefix.split("/")):
        raise ValueError("--path-prefix must be a non-empty relative path")
    if args.fixture and not args.dry_run:
        raise ValueError("--fixture is only allowed with --dry-run so it cannot publish test data")
    github_state_path = args.github_state_path.strip("/") if args.github_state_path else None
    if github_state_path and any(part in {"", ".", ".."} for part in github_state_path.split("/")):
        raise ValueError("--github-state-path must be a relative repository path")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    github_token = os.environ.get("GITHUB_TOKEN")
    if not args.dry_run and not args.fixture:
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
        if not github_token:
            raise ValueError("GITHUB_TOKEN environment variable is required")
    return Config(
        bot_token=bot_token,
        github_token=github_token,
        channel_id=args.channel_id,
        repository=args.repo,
        branch=args.branch,
        path_prefix=prefix,
        state_file=Path(args.state_file),
        github_state_path=github_state_path,
        poll_timeout=args.poll_timeout,
        once=args.once,
        dry_run=args.dry_run,
        fixture=args.fixture,
    )


def redact_url(url: str) -> str:
    return re.sub(r"/bot[^/]+/", "/bot***REDACTED***/", url)


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    allowed_statuses: set[int] | None = None,
) -> tuple[int, Any]:
    """Call JSON APIs with bounded retries and Telegram 429 handling."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    allowed_statuses = allowed_statuses or set()
    last_error: Exception | None = None

    for attempt in range(RETRY_COUNT + 1):
        try:
            request = Request(url, data=body, headers=request_headers, method=method)
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 -- fixed HTTPS API bases
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(response_body)
            except json.JSONDecodeError:
                parsed = {"description": response_body[:200]}
            if exc.code in allowed_statuses:
                return exc.code, parsed
            retry_after = 0
            if exc.code == 429 and isinstance(parsed, dict):
                retry_after = int(parsed.get("parameters", {}).get("retry_after", 0) or 0)
            retryable = exc.code == 429 or exc.code >= 500
            last_error = ApiError(f"{method} {redact_url(url)} returned HTTP {exc.code}")
            if retryable and attempt < RETRY_COUNT:
                delay = retry_after if retry_after > 0 else RETRY_DELAY_SECONDS * (attempt + 1)
                logging.warning("remote API returned %s; retrying in %.1fs", exc.code, delay)
                time.sleep(delay)
                continue
            raise last_error from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = ApiError(f"{method} {redact_url(url)} failed: {type(exc).__name__}")
            if attempt < RETRY_COUNT:
                delay = RETRY_DELAY_SECONDS * (attempt + 1)
                logging.warning("remote request failed; retrying in %.1fs", delay)
                time.sleep(delay)
                continue
            raise last_error from exc
    raise last_error or ApiError("unexpected HTTP retry state")


def telegram_call(config: Config, method: str, payload: dict[str, Any], *, timeout: int) -> Any:
    if not config.bot_token:
        raise ApiError("TELEGRAM_BOT_TOKEN is unavailable")
    url = f"{TELEGRAM_API}/bot{config.bot_token}/{method}"
    _status, response = http_json(url, method="POST", payload=payload, timeout=timeout)
    if not isinstance(response, dict) or not response.get("ok"):
        description = response.get("description", "unknown Telegram error") if isinstance(response, dict) else "invalid Telegram response"
        raise ApiError(f"Telegram {method} failed: {description}")
    return response.get("result")


def load_local_state(path: Path) -> dict[str, int]:
    if not path.exists():
        return {"next_offset": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        offset = int(data.get("next_offset", 0))
        return {"next_offset": max(offset, 0)}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"state file is invalid: {path}") from exc


def save_local_state(path: Path, next_offset: int, *, dry_run: bool) -> None:
    if dry_run:
        logging.info("dry run: would advance local update offset to %s", next_offset)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    payload = {"next_offset": next_offset, "updated_at": datetime.now(UTC).isoformat()}
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)


def has_media(message: dict[str, Any]) -> bool:
    return any(field in message for field in MEDIA_FIELDS)


def message_text(message: dict[str, Any]) -> str:
    value = message.get("text") or message.get("caption") or ""
    return value if isinstance(value, str) else ""


def publication_risks(text: str) -> list[str]:
    return [name for name, pattern in RISK_PATTERNS.items() if pattern.search(text)]


def markdown_quote(text: str) -> str:
    clipped = text[:MAX_MESSAGE_CHARS]
    if len(text) > MAX_MESSAGE_CHARS:
        clipped += "\n\n[内容因长度限制已截断]"
    return "\n".join(">" if not line else f"> {line}" for line in clipped.splitlines())


def post_path(config: Config, message: dict[str, Any]) -> str:
    message_id = int(message["message_id"])
    timestamp = datetime.fromtimestamp(int(message["date"]), tz=UTC)
    return f"{config.path_prefix}/{timestamp:%Y/%m/%d}/{message_id}.md"


def render_markdown(message: dict[str, Any]) -> str:
    timestamp = datetime.fromtimestamp(int(message["date"]), tz=UTC).isoformat()
    message_id = int(message["message_id"])
    text = message_text(message)
    return (
        f"# Telegram 频道消息 #{message_id}\n\n"
        f"- 发布时间（UTC）：`{timestamp}`\n"
        "- 来源：Telegram 频道\n"
        "- 发布方式：自动归档（已通过基础敏感信息检查）\n\n"
        "## 内容\n\n"
        f"{markdown_quote(text)}\n\n"
        "---\n\n"
        "看么科技客服 @hwxc129  \n"
        "看么科技频道 @hwxc131\n"
    )


def github_headers(token: str | None) -> dict[str, str]:
    if not token:
        raise ApiError("GITHUB_TOKEN is unavailable")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }


def github_get_file(config: Config, file_path: str) -> dict[str, Any] | None:
    encoded_path = quote(file_path, safe="/")
    url = f"{GITHUB_API}/repos/{config.repository}/contents/{encoded_path}?ref={quote(config.branch, safe='')}"
    status, body = http_json(url, headers=github_headers(config.github_token), allowed_statuses={404})
    if status == 404:
        return None
    if not isinstance(body, dict) or body.get("type") != "file":
        raise ApiError(f"GitHub returned an unexpected response for {file_path}")
    return body


def github_put_file(
    config: Config,
    file_path: str,
    content: str,
    commit_message: str,
    *,
    sha: str | None = None,
) -> None:
    """Create or replace one repository file, serially and with an optional SHA."""
    encoded_path = quote(file_path, safe="/")
    url = f"{GITHUB_API}/repos/{config.repository}/contents/{encoded_path}"
    payload: dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": config.branch,
    }
    if sha:
        payload["sha"] = sha
    status, _body = http_json(
        url,
        method="PUT",
        headers=github_headers(config.github_token),
        payload=payload,
        allowed_statuses={409, 422},
    )
    if status not in {200, 201}:
        raise ApiError(f"GitHub refused {file_path} with HTTP {status}; no update offset was advanced")


def create_github_file(config: Config, file_path: str, content: str, message_id: int) -> None:
    github_put_file(config, file_path, content, f"docs: archive Telegram post {message_id}")


def decode_github_file(file_data: dict[str, Any], file_path: str) -> str:
    try:
        content = file_data["content"].replace("\n", "")
        return base64.b64decode(content).decode("utf-8")
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ApiError(f"GitHub state file is not valid UTF-8 JSON: {file_path}") from exc


def load_github_state(config: Config) -> dict[str, int]:
    if not config.github_state_path:
        return {"next_offset": 0}
    if config.dry_run:
        logging.info("dry run: would read GitHub state from %s", config.github_state_path)
        return {"next_offset": 0}
    file_data = github_get_file(config, config.github_state_path)
    if file_data is None:
        return {"next_offset": 0}
    try:
        data = json.loads(decode_github_file(file_data, config.github_state_path))
        return {"next_offset": max(int(data.get("next_offset", 0)), 0)}
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError(f"GitHub state file is invalid: {config.github_state_path}") from exc


def save_github_state(config: Config, next_offset: int) -> None:
    if not config.github_state_path:
        return
    if config.dry_run:
        logging.info("dry run: would advance GitHub update offset to %s", next_offset)
        return
    existing = github_get_file(config, config.github_state_path)
    existing_sha = existing.get("sha") if existing else None
    content = json.dumps(
        {"next_offset": next_offset, "updated_at": datetime.now(UTC).isoformat()}, indent=2
    ) + "\n"
    github_put_file(
        config,
        config.github_state_path,
        content,
        "chore: advance Telegram sync offset",
        sha=existing_sha,
    )


def load_state(config: Config) -> dict[str, int]:
    return load_github_state(config) if config.github_state_path else load_local_state(config.state_file)


def save_state(config: Config, next_offset: int) -> None:
    if config.github_state_path:
        save_github_state(config, next_offset)
    else:
        save_local_state(config.state_file, next_offset, dry_run=config.dry_run)


def archive_channel_post(config: Config, message: dict[str, Any], summary: Summary) -> None:
    message_id = int(message.get("message_id", 0))
    if has_media(message):
        summary.skipped_media += 1
        logging.warning("skipped channel message %s because media publishing is disabled", message_id)
        return
    text = message_text(message)
    if not text.strip():
        summary.ignored += 1
        logging.info("ignored empty channel message %s", message_id)
        return
    risks = publication_risks(text)
    if risks:
        summary.skipped_risky += 1
        logging.warning("skipped channel message %s because of %s", message_id, ", ".join(risks))
        return

    file_path = post_path(config, message)
    if config.dry_run:
        summary.created += 1
        logging.info("dry run: would create %s for channel message %s", file_path, message_id)
        return
    if github_get_file(config, file_path) is not None:
        summary.already_exists += 1
        logging.info("GitHub file already exists for channel message %s", message_id)
        return
    create_github_file(config, file_path, render_markdown(message), message_id)
    summary.created += 1
    logging.info("archived channel message %s to %s", message_id, file_path)


def get_updates(config: Config, next_offset: int) -> list[dict[str, Any]]:
    if config.fixture:
        fixture_data = json.loads(config.fixture.read_text(encoding="utf-8"))
        if not fixture_data.get("ok"):
            raise ApiError("fixture does not contain an ok Telegram response")
        result = fixture_data.get("result", [])
        return result if isinstance(result, list) else []
    result = telegram_call(
        config,
        "getUpdates",
        {
            "offset": next_offset or None,
            "timeout": config.poll_timeout,
            "allowed_updates": ["channel_post"],
        },
        timeout=config.poll_timeout + 20,
    )
    return result if isinstance(result, list) else []


def ensure_polling_is_available(config: Config) -> None:
    if config.fixture or config.dry_run:
        return
    info = telegram_call(config, "getWebhookInfo", {}, timeout=30)
    if isinstance(info, dict) and info.get("url"):
        raise ApiError(
            "this bot already has a webhook; getUpdates cannot run at the same time. "
            "Keep the existing webhook or deploy a webhook receiver instead—this tool will not remove it."
        )


def run(config: Config) -> Summary:
    ensure_polling_is_available(config)
    state = load_state(config)
    summary = Summary()
    last_health_log = time.monotonic()

    while not shutdown_requested:
        updates = get_updates(config, state["next_offset"])
        for update in updates:
            if shutdown_requested:
                break
            update_id = int(update.get("update_id", 0))
            if update_id < state["next_offset"]:
                continue
            message = update.get("channel_post")
            if not isinstance(message, dict) or int(message.get("chat", {}).get("id", 0)) != config.channel_id:
                summary.ignored += 1
                logging.info("ignored unrelated update %s", update_id)
            else:
                archive_channel_post(config, message, summary)
            state["next_offset"] = update_id + 1
            save_state(config, state["next_offset"])

        if config.once or config.fixture:
            break
        now = time.monotonic()
        if now - last_health_log >= 300:
            logging.info("heartbeat rss_bytes=%s next_offset=%s", _rss_bytes(), state["next_offset"])
            last_health_log = now

    return summary


def _rss_bytes() -> int:
    """Return resident memory when supported, without adding a dependency."""
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, AttributeError):
        return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    try:
        config = make_config(args)
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        summary = run(config)
        logging.info(
            "summary created=%s exists=%s skipped_risky=%s skipped_media=%s ignored=%s",
            summary.created,
            summary.already_exists,
            summary.skipped_risky,
            summary.skipped_media,
            summary.ignored,
        )
        return 0
    except (ApiError, OSError, ValueError, json.JSONDecodeError) as exc:
        logging.error("monitor stopped: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
