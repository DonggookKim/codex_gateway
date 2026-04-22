from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class RunMode(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass
class LastRunSummary:
    run_id: str
    requester_user_id: int
    requester_name: str
    prompt_excerpt: str
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    exit_signal: str | None
    stdout_excerpt: str
    stderr_excerpt: str
    assistant_response_excerpt: str

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["started_at"] = isoformat_or_none(self.started_at)
        payload["finished_at"] = isoformat_or_none(self.finished_at)
        return payload

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> "LastRunSummary":
        return cls(
            run_id=str(payload.get("run_id", "")),
            requester_user_id=int(payload.get("requester_user_id", 0)),
            requester_name=str(payload.get("requester_name", "")),
            prompt_excerpt=str(payload.get("prompt_excerpt", "")),
            started_at=parse_datetime(payload.get("started_at")),
            finished_at=parse_datetime(payload.get("finished_at")),
            exit_code=(
                int(payload["exit_code"])
                if payload.get("exit_code") is not None
                else None
            ),
            exit_signal=(
                str(payload["exit_signal"])
                if payload.get("exit_signal") is not None
                else None
            ),
            stdout_excerpt=str(payload.get("stdout_excerpt", "")),
            stderr_excerpt=str(payload.get("stderr_excerpt", "")),
            assistant_response_excerpt=str(
                payload.get("assistant_response_excerpt", "")
            ),
        )


@dataclass
class ActiveRun:
    run_id: str
    requester_user_id: int
    requester_name: str
    prompt_excerpt: str
    started_at: datetime
    pid: int | None
    last_message_path: Path
    stdout_tail: str = ""
    stderr_tail: str = ""
    stop_requested_at: datetime | None = None
    process: asyncio.subprocess.Process | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)


class GatewayState:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.run_lock = asyncio.Lock()
        self.mode = RunMode.IDLE
        self.active_run: ActiveRun | None = None
        self.last_run = self._load_last_run()

    def _load_last_run(self) -> LastRunSummary | None:
        if not self.state_file.exists():
            return None
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        last_run = payload.get("last_run")
        if not isinstance(last_run, dict):
            return None
        try:
            return LastRunSummary.from_json_dict(last_run)
        except (TypeError, ValueError):
            return None

    def _persist_last_run(self) -> None:
        payload = {
            "last_run": self.last_run.to_json_dict() if self.last_run else None,
        }
        tmp_path = self.state_file.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.state_file)

    def set_active_run(self, active_run: ActiveRun) -> None:
        self.active_run = active_run
        self.mode = RunMode.RUNNING

    def mark_stopping(self) -> None:
        if self.active_run is None:
            return
        self.active_run.stop_requested_at = utc_now()
        self.mode = RunMode.STOPPING

    def finish_run(self, summary: LastRunSummary) -> None:
        self.last_run = summary
        self.active_run = None
        self.mode = RunMode.IDLE
        self._persist_last_run()

