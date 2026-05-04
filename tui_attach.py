from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .project_registry import ProjectRegistry
from .runner import prepare_runtime_home_dir
from .session_store import SessionRecord, SessionStore


THREAD_ID_PATTERN = re.compile(
    r'"thread_id"\s*:\s*"(?P<thread_id>[^"]+)"|'
    r'"payload"\s*:\s*\{\s*"id"\s*:\s*"(?P<payload_id>[^"]+)"'
)


class TuiAttachError(ValueError):
    pass


@dataclass(frozen=True)
class AttachTarget:
    project_id: str
    session_id: str
    project_cwd: Path
    home_parent: Path
    thread_ref: str
    model_profile: str
    execution_env: str


def infer_gateway_home_parent(runtime_root: Path) -> Path:
    current = runtime_root
    while True:
        if current.name == "projects":
            return current.parent / "codex-home"
        if current.parent == current:
            raise TuiAttachError(
                f"Unable to infer gateway HOME from runtime root: {runtime_root}"
            )
        current = current.parent


def resolve_codex_thread_ref(session: SessionRecord) -> str | None:
    if session.codex_thread_ref:
        return session.codex_thread_ref
    if not session.last_run_summary:
        return None

    for key in ("codex_thread_ref", "stdout_excerpt"):
        raw = session.last_run_summary.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        if key == "codex_thread_ref":
            return raw.strip()
        match = THREAD_ID_PATTERN.search(raw)
        if match:
            return match.group("thread_id") or match.group("payload_id")
    return None


def _thread_ref_exists_in_home(home_parent: Path, thread_ref: str) -> bool:
    sessions_root = home_parent / ".codex" / "sessions"
    if not sessions_root.exists():
        return False
    for session_file in sessions_root.rglob("*.jsonl"):
        if thread_ref in session_file.name:
            return True
        try:
            text = session_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if thread_ref in text:
            return True
    return False


def _debug_home_parent(session: SessionRecord) -> Path | None:
    debug_path = session.last_response_path.with_name("last_debug.json")
    try:
        payload = json.loads(debug_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    home = payload.get("home")
    if not isinstance(home, str) or not home.strip():
        return None
    return Path(home.strip())


def resolve_attach_target(
    *,
    state_root: Path,
    projects_file: Path,
    project_id: str,
    session_id: str,
) -> AttachTarget:
    store = SessionStore(state_root)
    session = store.load_session(project_id, session_id)
    thread_ref = resolve_codex_thread_ref(session)
    if not thread_ref:
        raise TuiAttachError(
            "No Codex thread reference is recorded for this session yet."
        )
    home_candidates: list[Path] = []
    if str(session.codex_home_path).strip():
        home_candidates.append(session.codex_home_path.parent)
    debug_home_parent = _debug_home_parent(session)
    if debug_home_parent is not None and debug_home_parent not in home_candidates:
        home_candidates.append(debug_home_parent)
    inferred_home_parent = infer_gateway_home_parent(session.runtime_root)
    if inferred_home_parent not in home_candidates:
        home_candidates.append(inferred_home_parent)

    home_parent = next(
        (
            candidate
            for candidate in home_candidates
            if _thread_ref_exists_in_home(candidate, thread_ref)
        ),
        None,
    )
    if home_parent is None:
        raise TuiAttachError(
            "Recorded Codex thread reference is not present in the gateway HOME."
        )

    registry = ProjectRegistry(projects_file)
    project = next(
        (
            item
            for item in registry.load_projects()
            if item.project_id == project_id and not item.archived
        ),
        None,
    )
    if project is None:
        raise TuiAttachError(f"Project is unavailable: {project_id}")

    return AttachTarget(
        project_id=project_id,
        session_id=session_id,
        project_cwd=project.cwd,
        home_parent=home_parent,
        thread_ref=thread_ref,
        model_profile=session.model_profile,
        execution_env=session.execution_env,
    )


def build_attach_command(
    *,
    repo_root: Path,
    project_id: str,
    session: SessionRecord,
) -> str:
    if not resolve_codex_thread_ref(session):
        raise TuiAttachError(
            "No Codex thread reference is recorded for this session yet."
        )
    script_path = repo_root / "codex_gateway" / "attach-gateway-session.sh"
    return f"bash {script_path} {project_id} {session.session_id}"


def build_resume_argv(target: AttachTarget) -> list[str]:
    argv = [
        "codex",
        "resume",
    ]
    if target.model_profile:
        argv.extend(["-m", target.model_profile])
    argv.extend(
        [
            "--include-non-interactive",
            "--all",
            "-C",
            str(target.project_cwd),
            target.thread_ref,
        ]
    )
    return argv


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attach a local Codex TUI to a gateway-managed session."
    )
    parser.add_argument("project_id")
    parser.add_argument("session_id")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--projects-file", type=Path, required=True)
    parser.add_argument("--print", action="store_true", dest="print_only")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    target = resolve_attach_target(
        state_root=args.state_root,
        projects_file=args.projects_file,
        project_id=args.project_id,
        session_id=args.session_id,
    )
    argv = build_resume_argv(target)
    if args.print_only:
        print("HOME=" + str(target.home_parent))
        print("ARGV=" + " ".join(argv))
        return

    prepare_runtime_home_dir(
        home_parent=target.home_parent,
        seed_from=None,
        use_shared_auth=True,
    )
    child_env = dict(os.environ)
    child_env["HOME"] = str(target.home_parent)
    os.execvpe(argv[0], argv, child_env)


if __name__ == "__main__":
    main()
