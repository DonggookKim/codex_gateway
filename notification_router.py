from __future__ import annotations

from .formatter import limit_discord_message


def build_project_notification(
    project_label: str,
    session_label: str,
    reason_code: str,
    explanation: str,
    recovery_hint: str,
) -> str:
    lines = [
        f"Project: `{project_label}`",
        f"Session: `{session_label}`",
        f"Blocked: `{reason_code}`",
    ]
    if explanation.strip():
        lines.append("Reason: " + explanation.strip())
    if recovery_hint.strip():
        lines.append("Next action: " + recovery_hint.strip())
    return limit_discord_message("\n".join(lines))
