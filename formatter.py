from __future__ import annotations

from datetime import datetime, timezone

from .session_inspector import LatestCodexResponse
from .state import GatewayState, LastRunSummary, RunMode


DISCORD_TEXT_LIMIT = 1800


def excerpt(text: str | None, max_chars: int) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def inline_excerpt(text: str | None, max_chars: int) -> str:
    return excerpt(text, max_chars).replace("\n", " ")


def response_preview(text: str | None, max_chars: int) -> str:
    raw = (text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if not raw:
        return ""
    single_line = raw.replace("\n", " ")
    if len(single_line) <= max_chars:
        return single_line
    return single_line[:max_chars].rstrip() + "…"


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_elapsed(started_at: datetime | None) -> str:
    if started_at is None:
        return "n/a"
    delta = datetime.now(timezone.utc) - started_at
    total_seconds = max(int(delta.total_seconds()), 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def limit_discord_message(text: str) -> str:
    return excerpt(text, DISCORD_TEXT_LIMIT)


def format_status(
    state: GatewayState,
    text_limit: int,
    latest_response: LatestCodexResponse | None = None,
    response_preview_chars: int = 20,
    external_codex_activity: bool = False,
) -> str:
    local_activity_line = (
        "Local Codex activity: "
        + ("`active`" if external_codex_activity else "`idle`")
    )
    if state.active_run is not None:
        active = state.active_run
        lines = [
            f"State: `{state.mode.value}`",
            local_activity_line,
            f"Run ID: `{active.run_id}`",
            f"Requester: `{active.requester_name}` (`{active.requester_user_id}`)",
            f"PID: `{active.pid or 'n/a'}`",
            f"Started: `{_fmt_time(active.started_at)}`",
            f"Elapsed: `{_fmt_elapsed(active.started_at)}`",
            f"Current request: {inline_excerpt(active.prompt_excerpt, text_limit)}",
        ]
        if latest_response is not None and latest_response.text.strip():
            lines.append(
                "Last Codex response: "
                + response_preview(latest_response.text, response_preview_chars)
            )
        if active.stderr_tail.strip():
            lines.append(
                "Recent stderr: "
                + inline_excerpt(active.stderr_tail, text_limit)
            )
        return limit_discord_message("\n".join(lines))

    last_run = state.last_run
    if last_run is None:
        return "\n".join(
            [
                "State: `idle`",
                local_activity_line,
                "No previous gateway-managed run recorded yet.",
            ]
        )

    lines = [
        f"State: `{RunMode.IDLE.value}`",
        local_activity_line,
        f"Last run ID: `{last_run.run_id}`",
        f"Last requester: `{last_run.requester_name}` (`{last_run.requester_user_id}`)",
        f"Started: `{_fmt_time(last_run.started_at)}`",
        f"Finished: `{_fmt_time(last_run.finished_at)}`",
    ]
    if latest_response is not None and latest_response.text.strip():
        lines.append(f"Last Codex session: `{latest_response.session_id}`")
        lines.append(
            "Last Codex response: "
            + response_preview(latest_response.text, response_preview_chars)
        )
    elif last_run.assistant_response_excerpt.strip():
        lines.append(
            "Last gateway response: "
            + response_preview(
                last_run.assistant_response_excerpt,
                response_preview_chars,
            )
        )
    if last_run.exit_signal:
        lines.append(f"Exit signal: `{last_run.exit_signal}`")
    else:
        lines.append(f"Exit code: `{last_run.exit_code}`")
    if last_run.stderr_excerpt.strip() and (
        last_run.exit_code not in (0, None) or not last_run.assistant_response_excerpt.strip()
    ):
        lines.append(
            "Last stderr: " + inline_excerpt(last_run.stderr_excerpt, text_limit)
        )
    return limit_discord_message("\n".join(lines))


def format_run_complete(
    summary: LastRunSummary,
    text_limit: int,
    response_preview_chars: int = 20,
) -> str:
    lines = [
        f"Run complete: `{summary.run_id}`",
        f"Requester: `{summary.requester_name}`",
    ]
    if summary.exit_signal:
        lines.append(f"Exit signal: `{summary.exit_signal}`")
    else:
        lines.append(f"Exit code: `{summary.exit_code}`")
    if summary.assistant_response_excerpt.strip():
        lines.append(
            "Last response: "
            + response_preview(
                summary.assistant_response_excerpt,
                response_preview_chars,
            )
        )
    if summary.stderr_excerpt.strip() and (
        summary.exit_code not in (0, None) or not summary.assistant_response_excerpt.strip()
    ):
        lines.append(
            "stderr: " + inline_excerpt(summary.stderr_excerpt, text_limit)
        )
    return limit_discord_message("\n".join(lines))


def format_run_failure(summary: LastRunSummary, text_limit: int) -> str:
    lines = [
        f"Run failed to start: `{summary.run_id}`",
        f"Requester: `{summary.requester_name}`",
    ]
    if summary.stderr_excerpt.strip():
        lines.append(
            "Reason: " + inline_excerpt(summary.stderr_excerpt, text_limit)
        )
    return limit_discord_message("\n".join(lines))
