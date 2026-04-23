from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectDefinition:
    project_id: str
    label: str
    cwd: Path
    project_channel_id: int
    default_model_profile: str
    allowed_model_profiles: list[str]
    active_session_id: str | None
    archived: bool

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["cwd"] = str(self.cwd)
        return payload

    @classmethod
    def from_json_dict(cls, payload: dict[str, object]) -> "ProjectDefinition":
        project_id = str(payload.get("project_id", "")).strip()
        if not project_id:
            raise ValueError("Project definition is missing project_id")

        cwd_value = payload.get("cwd")
        if not isinstance(cwd_value, str) or not cwd_value.strip():
            raise ValueError(f"Project {project_id} is missing cwd")

        return cls(
            project_id=project_id,
            label=str(payload.get("label", "")).strip(),
            cwd=Path(cwd_value).expanduser(),
            project_channel_id=int(payload.get("project_channel_id", 0)),
            default_model_profile=str(payload.get("default_model_profile", "")).strip(),
            allowed_model_profiles=[
                str(item)
                for item in payload.get("allowed_model_profiles", [])
            ],
            active_session_id=(
                str(payload["active_session_id"])
                if payload.get("active_session_id") is not None
                else None
            ),
            archived=bool(payload.get("archived", False)),
        )


class ProjectRegistry:
    def __init__(self, projects_file: Path) -> None:
        self.projects_file = projects_file

    def load_projects(self) -> list[ProjectDefinition]:
        if not self.projects_file.exists():
            return []

        try:
            payload = json.loads(self.projects_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Failed to load project registry: {self.projects_file}"
            ) from exc

        if not isinstance(payload, dict):
            raise ValueError("Project registry must store a JSON object")

        raw_projects = payload.get("projects", [])
        if not isinstance(raw_projects, list):
            raise ValueError("Project registry must store projects as a list")

        projects: list[ProjectDefinition] = []
        for project_payload in raw_projects:
            if not isinstance(project_payload, dict):
                raise ValueError("Each project definition must be a JSON object")
            projects.append(ProjectDefinition.from_json_dict(project_payload))
        return projects

    def save_projects(self, projects: list[ProjectDefinition]) -> None:
        self._validate_unique_project_ids(projects)
        self.projects_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "projects": [
                project_definition.to_json_dict() for project_definition in projects
            ]
        }
        tmp_path = self.projects_file.with_suffix(self.projects_file.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.projects_file)

    def _validate_unique_project_ids(self, projects: list[ProjectDefinition]) -> None:
        seen_project_ids: set[str] = set()
        duplicate_project_ids: set[str] = set()
        for project_definition in projects:
            if project_definition.project_id in seen_project_ids:
                duplicate_project_ids.add(project_definition.project_id)
            else:
                seen_project_ids.add(project_definition.project_id)

        if duplicate_project_ids:
            duplicates = ", ".join(sorted(duplicate_project_ids))
            raise ValueError(f"Duplicate project_id values are not allowed: {duplicates}")
