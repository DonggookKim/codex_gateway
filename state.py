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


def _create_lock() -> asyncio.Lock:
    try:
        return asyncio.Lock()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return asyncio.Lock()


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
    codex_thread_ref: str | None = None

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
            codex_thread_ref=(
                str(payload["codex_thread_ref"])
                if payload.get("codex_thread_ref") is not None
                else None
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
    project_id: str | None = None
    session_id: str | None = None
    model_profile: str | None = None
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
        self.run_lock = _create_lock()
        self.project_run_locks: dict[str, asyncio.Lock] = {}
        self.active_runs: dict[str, ActiveRun] = {}
        self.active_run_project_id: str | None = None
        self.project_last_runs: dict[str, LastRunSummary] = {}
        self.selection_state: dict[str, str | None] = {
            "selected_project_id": None,
            "selected_session_id": None,
            "selected_model_profile_for_new_session": None,
            "watched_project_id": None,
            "watched_session_id": None,
            "watched_run_id": None,
            "watch_interval_seconds": None,
        }
        self.mode = RunMode.IDLE
        self.active_run: ActiveRun | None = None
        self.last_run = self._load_last_run()
        self._load_selection_state()

    def _load_last_run(self) -> LastRunSummary | None:
        if not self.state_file.exists():
            return None
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        last_run = payload.get("last_run")
        if not isinstance(last_run, dict):
            last_run = None
        if last_run is None:
            return None
        try:
            return LastRunSummary.from_json_dict(last_run)
        except (TypeError, ValueError):
            return None

    def _load_selection_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return

        selection_state = payload.get("selection_state")
        if isinstance(selection_state, dict):
            for key in self.selection_state:
                value = selection_state.get(key)
                self.selection_state[key] = (
                    str(value) if value is not None else None
                )

        project_last_runs = payload.get("project_last_runs")
        if isinstance(project_last_runs, dict):
            for project_id, raw_summary in project_last_runs.items():
                if not isinstance(raw_summary, dict):
                    continue
                try:
                    self.project_last_runs[str(project_id)] = (
                        LastRunSummary.from_json_dict(raw_summary)
                    )
                except (TypeError, ValueError):
                    continue

    def _persist_last_run(self) -> None:
        payload = {
            "last_run": self.last_run.to_json_dict() if self.last_run else None,
            "project_last_runs": {
                project_id: summary.to_json_dict()
                for project_id, summary in self.project_last_runs.items()
            },
            "selection_state": self.selection_state,
        }
        tmp_path = self.state_file.with_suffix(".tmp")
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.state_file)

    def get_project_run_lock(self, project_id: str) -> asyncio.Lock:
        lock = self.project_run_locks.get(project_id)
        if lock is None:
            lock = _create_lock()
            self.project_run_locks[project_id] = lock
        return lock

    def select_project(self, project_id: str | None) -> None:
        if self.selection_state.get("watched_project_id") not in (None, project_id):
            self.disable_watch()
        self.selection_state["selected_project_id"] = project_id
        self.selection_state["selected_session_id"] = None
        self._persist_last_run()

    def select_session(self, session_id: str | None) -> None:
        if self.selection_state.get("watched_session_id") not in (None, session_id):
            self.disable_watch()
        self.selection_state["selected_session_id"] = session_id
        self._persist_last_run()

    def select_model_profile_for_new_session(self, model_profile: str | None) -> None:
        self.selection_state["selected_model_profile_for_new_session"] = model_profile
        self._persist_last_run()

    def enable_watch(
        self,
        project_id: str,
        session_id: str,
        *,
        run_id: str | None = None,
        interval_seconds: float = 600.0,
    ) -> None:
        self.selection_state["watched_project_id"] = project_id
        self.selection_state["watched_session_id"] = session_id
        self.selection_state["watched_run_id"] = run_id
        self.selection_state["watch_interval_seconds"] = str(interval_seconds)
        self._persist_last_run()

    def disable_watch(self) -> None:
        self.selection_state["watched_project_id"] = None
        self.selection_state["watched_session_id"] = None
        self.selection_state["watched_run_id"] = None
        self.selection_state["watch_interval_seconds"] = None
        self._persist_last_run()

    def set_watched_run_id(self, run_id: str | None) -> None:
        self.selection_state["watched_run_id"] = run_id
        self._persist_last_run()

    def set_active_run(
        self,
        active_run: ActiveRun,
        project_id: str | None = None,
    ) -> None:
        project_key = project_id or self.selection_state["selected_project_id"] or "global"
        self.active_runs[project_key] = active_run
        self.active_run_project_id = project_key
        self.active_run = active_run
        self.mode = RunMode.RUNNING

    def mark_stopping(self, project_id: str | None = None) -> None:
        if self.active_run is None:
            return
        self.active_run.stop_requested_at = utc_now()
        self.mode = RunMode.STOPPING
        project_key = project_id or self.active_run_project_id
        if project_key is not None and project_key in self.active_runs:
            self.active_runs[project_key].stop_requested_at = (
                self.active_run.stop_requested_at
            )

    def finish_run(self, summary: LastRunSummary, project_id: str | None = None) -> None:
        project_key = project_id or self.active_run_project_id or self.selection_state["selected_project_id"] or "global"
        self.last_run = summary
        self.project_last_runs[project_key] = summary
        self.active_runs.pop(project_key, None)
        if self.active_run_project_id == project_key:
            self.active_run = None
            self.active_run_project_id = None
        elif project_id is None and not self.active_runs:
            self.active_run = None
            self.active_run_project_id = None
        self.mode = RunMode.IDLE
        self._persist_last_run()
