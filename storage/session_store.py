from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .execution_env import infer_execution_env


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionRecord:
    session_id: str
    project_id: str
    label: str
    model_profile: str
    execution_env: str
    status: str
    codex_home_path: Path
    runtime_root: Path
    last_response_path: Path
    codex_thread_ref: str | None
    last_run_summary: dict[str, object] | None
    waiting_on: str | None
    blocking_reason: str | None
    recovery_hint: str | None
    started_waiting_at: str | None
    expires_at: str | None
    expired_at: str | None
    last_notified_at: str | None
    last_prompt_excerpt: str | None
    created_at: str
    last_active_at: str
    archived: bool
    backend: str = "codex"

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["codex_home_path"] = str(self.codex_home_path)
        payload["runtime_root"] = str(self.runtime_root)
        payload["last_response_path"] = str(self.last_response_path)
        return payload

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> "SessionRecord":
        return cls(
            session_id=str(payload.get("session_id", "")),
            project_id=str(payload.get("project_id", "")),
            label=str(payload.get("label", "")),
            model_profile=str(payload.get("model_profile", "")),
            execution_env=str(
                payload.get(
                    "execution_env",
                    infer_execution_env(str(payload.get("model_profile", ""))),
                )
            ),
            backend=str(payload.get("backend", "codex")),
            status=str(payload.get("status", "idle")),
            codex_home_path=Path(str(payload.get("codex_home_path", ""))),
            runtime_root=Path(str(payload.get("runtime_root", ""))),
            last_response_path=Path(str(payload.get("last_response_path", ""))),
            codex_thread_ref=(
                str(payload["codex_thread_ref"])
                if payload.get("codex_thread_ref") is not None
                else None
            ),
            last_run_summary=payload.get("last_run_summary")
            if isinstance(payload.get("last_run_summary"), dict)
            else None,
            waiting_on=(
                str(payload["waiting_on"])
                if payload.get("waiting_on") is not None
                else None
            ),
            blocking_reason=(
                str(payload["blocking_reason"])
                if payload.get("blocking_reason") is not None
                else None
            ),
            recovery_hint=(
                str(payload["recovery_hint"])
                if payload.get("recovery_hint") is not None
                else None
            ),
            started_waiting_at=(
                str(payload["started_waiting_at"])
                if payload.get("started_waiting_at") is not None
                else None
            ),
            expires_at=(
                str(payload["expires_at"])
                if payload.get("expires_at") is not None
                else None
            ),
            expired_at=(
                str(payload["expired_at"])
                if payload.get("expired_at") is not None
                else None
            ),
            last_notified_at=(
                str(payload["last_notified_at"])
                if payload.get("last_notified_at") is not None
                else None
            ),
            last_prompt_excerpt=(
                str(payload["last_prompt_excerpt"])
                if payload.get("last_prompt_excerpt") is not None
                else None
            ),
            created_at=str(payload.get("created_at", utc_now())),
            last_active_at=str(payload.get("last_active_at", utc_now())),
            archived=bool(payload.get("archived", False)),
        )


class SessionStore:
    def __init__(
        self,
        state_root: Path,
        runtime_root: Path | None = None,
        gateway_codex_home: Path | None = None,
    ) -> None:
        self.state_root = state_root
        self.runtime_root = runtime_root or (
            Path.home() / "codex_gateway_runtime"
        )
        self.gateway_codex_home = gateway_codex_home

    def create_session(
        self,
        project_id: str,
        label: str,
        model_profile: str,
        execution_env: str | None = None,
        backend: str = "codex",
    ) -> SessionRecord:
        session_id = uuid.uuid4().hex[:8]
        record = self._build_session_record(
            project_id=project_id,
            session_id=session_id,
            label=label,
            model_profile=model_profile,
            execution_env=execution_env or infer_execution_env(model_profile),
            codex_thread_ref=None,
            backend=backend,
        )
        self._write_record(record)
        return record

    def import_session(
        self,
        *,
        project_id: str,
        session_id: str,
        label: str,
        model_profile: str,
        execution_env: str | None = None,
        codex_thread_ref: str | None = None,
        backend: str = "codex",
    ) -> SessionRecord:
        record = self._build_session_record(
            project_id=project_id,
            session_id=session_id,
            label=label,
            model_profile=model_profile,
            execution_env=execution_env or infer_execution_env(model_profile),
            codex_thread_ref=codex_thread_ref,
            backend=backend,
        )
        self._write_record(record)
        return record

    def load_session(self, project_id: str, session_id: str) -> SessionRecord:
        record_path = self._record_path(project_id, session_id)
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Failed to load session record: {record_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Session record must be a JSON object")
        return SessionRecord.from_json_dict(payload)

    def list_sessions(self, project_id: str) -> list[SessionRecord]:
        sessions_root = self.state_root / "projects" / project_id / "sessions"
        if not sessions_root.exists():
            return []

        records: list[SessionRecord] = []
        for record_path in sorted(sessions_root.glob("*/session.json")):
            try:
                payload = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            try:
                records.append(SessionRecord.from_json_dict(payload))
            except ValueError:
                continue

        return sorted(
            records,
            key=lambda record: (
                record.last_active_at,
                record.created_at,
                record.session_id,
            ),
            reverse=True,
        )

    def mark_blocked(
        self,
        project_id: str,
        session_id: str,
        *,
        waiting_on: str,
        blocking_reason: str,
        recovery_hint: str,
    ) -> SessionRecord:
        record = self.load_session(project_id, session_id)
        blocked_record = SessionRecord(
            session_id=record.session_id,
            project_id=record.project_id,
            label=record.label,
            model_profile=record.model_profile,
            execution_env=record.execution_env,
            status="blocked",
            codex_home_path=record.codex_home_path,
            runtime_root=record.runtime_root,
            last_response_path=record.last_response_path,
            codex_thread_ref=record.codex_thread_ref,
            last_run_summary=record.last_run_summary,
            waiting_on=waiting_on,
            blocking_reason=blocking_reason,
            recovery_hint=recovery_hint,
            started_waiting_at=record.started_waiting_at or utc_now(),
            expires_at=record.expires_at,
            expired_at=record.expired_at,
            last_notified_at=record.last_notified_at,
            last_prompt_excerpt=record.last_prompt_excerpt,
            created_at=record.created_at,
            last_active_at=utc_now(),
            archived=record.archived,
            backend=record.backend,
        )
        self._write_record(blocked_record)
        return blocked_record

    def update_prompt_excerpt(
        self,
        project_id: str,
        session_id: str,
        prompt_excerpt: str,
    ) -> SessionRecord:
        record = self.load_session(project_id, session_id)
        updated_record = SessionRecord(
            **{
                **record.__dict__,
                "last_prompt_excerpt": prompt_excerpt,
                "last_active_at": utc_now(),
            }
        )
        self._write_record(updated_record)
        return updated_record

    def update_last_run_summary(
        self,
        project_id: str,
        session_id: str,
        summary: dict[str, object],
    ) -> SessionRecord:
        record = self.load_session(project_id, session_id)
        updated_record = SessionRecord(
            **{
                **record.__dict__,
                "last_run_summary": summary,
                "last_active_at": utc_now(),
            }
        )
        self._write_record(updated_record)
        return updated_record

    def update_codex_thread_ref(
        self,
        project_id: str,
        session_id: str,
        codex_thread_ref: str,
    ) -> SessionRecord:
        record = self.load_session(project_id, session_id)
        updated_record = SessionRecord(
            **{
                **record.__dict__,
                "codex_thread_ref": codex_thread_ref,
                "last_active_at": utc_now(),
            }
        )
        self._write_record(updated_record)
        return updated_record

    def _build_session_record(
        self,
        *,
        project_id: str,
        session_id: str,
        label: str,
        model_profile: str,
        execution_env: str,
        codex_thread_ref: str | None,
        backend: str = "codex",
    ) -> SessionRecord:
        session_root = self._session_root(project_id, session_id)
        codex_home_path = session_root / "codex-home" / ".codex"
        runtime_root = (
            self.runtime_root / "projects" / project_id / "sessions" / session_id
        )
        last_response_path = session_root / "artifacts" / "last_response.txt"
        codex_home_path.mkdir(parents=True, exist_ok=True)
        runtime_root.mkdir(parents=True, exist_ok=True)
        last_response_path.parent.mkdir(parents=True, exist_ok=True)
        return SessionRecord(
            session_id=session_id,
            project_id=project_id,
            label=label,
            model_profile=model_profile,
            execution_env=execution_env,
            status="idle",
            codex_home_path=codex_home_path,
            runtime_root=runtime_root,
            last_response_path=last_response_path,
            codex_thread_ref=codex_thread_ref,
            last_run_summary=None,
            waiting_on=None,
            blocking_reason=None,
            recovery_hint=None,
            started_waiting_at=None,
            expires_at=None,
            expired_at=None,
            last_notified_at=None,
            last_prompt_excerpt=None,
            created_at=utc_now(),
            last_active_at=utc_now(),
            archived=False,
            backend=backend,
        )

    def _write_record(self, record: SessionRecord) -> None:
        record_path = self._record_path(record.project_id, record.session_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = record_path.with_suffix(record_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(record.to_json_dict(), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(record_path)

    def _session_root(self, project_id: str, session_id: str) -> Path:
        return self.state_root / "projects" / project_id / "sessions" / session_id

    def _record_path(self, project_id: str, session_id: str) -> Path:
        return self._session_root(project_id, session_id) / "session.json"

    def _expected_codex_home_path(
        self,
        project_id: str,
        session_id: str,
    ) -> Path:
        return self._session_root(project_id, session_id) / "codex-home" / ".codex"

    def materialize_session_codex_home(self, record: SessionRecord) -> SessionRecord:
        expected_codex_home = self._expected_codex_home_path(
            record.project_id,
            record.session_id,
        )
        if record.codex_home_path == expected_codex_home:
            return record

        if record.codex_home_path.exists():
            self._copy_legacy_session_rollouts(
                source_codex_home=record.codex_home_path,
                target_codex_home=expected_codex_home,
                thread_ref=record.codex_thread_ref,
            )

        updated_record = SessionRecord(
            **{
                **record.__dict__,
                "codex_home_path": expected_codex_home,
            }
        )
        self._write_record(updated_record)
        return updated_record

    def _copy_legacy_session_rollouts(
        self,
        *,
        source_codex_home: Path,
        target_codex_home: Path,
        thread_ref: str | None,
    ) -> None:
        source_sessions = source_codex_home / "sessions"
        if not source_sessions.exists():
            return

        target_codex_home.mkdir(parents=True, exist_ok=True)
        copied_any = False
        for source_file in source_sessions.rglob("*.jsonl"):
            if not self._should_copy_rollout_file(source_file, thread_ref):
                continue
            relative_path = source_file.relative_to(source_codex_home)
            target_file = target_codex_home / relative_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            copied_any = True

        if not copied_any:
            sessions_dir = target_codex_home / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)

    def _should_copy_rollout_file(
        self,
        source_file: Path,
        thread_ref: str | None,
    ) -> bool:
        if not thread_ref:
            return False
        if thread_ref in source_file.name:
            return True
        try:
            return thread_ref in source_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return False
