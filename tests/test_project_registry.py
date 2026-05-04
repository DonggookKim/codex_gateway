from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_gateway.storage.project_registry import ProjectDefinition, ProjectRegistry


class ProjectRegistryTest(unittest.TestCase):
    def test_env_example_mentions_projects_file_and_state_root(self) -> None:
        text = Path("codex_gateway/.env.example").read_text(encoding="utf-8")

        self.assertIn("STATE_ROOT", text)
        self.assertIn("RUNTIME_ROOT", text)
        self.assertIn("PROJECTS_FILE", text)

    def test_load_returns_project_definitions_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_file = Path(temp_dir) / "projects.json"
            projects_file.write_text(
                """
                {
                  "projects": [
                    {
                      "project_id": "alpha",
                      "label": "Alpha",
                      "cwd": "/work/alpha",
                      "project_channel_id": 111,
                      "default_model_profile": "gpt-5.4",
                      "allowed_model_profiles": ["gpt-5.4", "gpt-5.2"],
                      "active_session_id": null,
                      "archived": false
                    },
                    {
                      "project_id": "beta",
                      "label": "Beta",
                      "cwd": "/work/beta",
                      "project_channel_id": 222,
                      "default_model_profile": "gpt-5.2",
                      "allowed_model_profiles": ["gpt-5.2"],
                      "active_session_id": "sess-1",
                      "archived": false
                    }
                  ]
                }
                """.strip(),
                encoding="utf-8",
            )

            registry = ProjectRegistry(projects_file)
            projects = registry.load_projects()

        self.assertEqual(
            projects,
            [
                ProjectDefinition(
                    project_id="alpha",
                    label="Alpha",
                    cwd=Path("/work/alpha"),
                    project_channel_id=111,
                    default_model_profile="gpt-5.4",
                    allowed_model_profiles=["gpt-5.4", "gpt-5.2"],
                    active_session_id=None,
                    archived=False,
                ),
                ProjectDefinition(
                    project_id="beta",
                    label="Beta",
                    cwd=Path("/work/beta"),
                    project_channel_id=222,
                    default_model_profile="gpt-5.2",
                    allowed_model_profiles=["gpt-5.2"],
                    active_session_id="sess-1",
                    archived=False,
                ),
            ],
        )

    def test_save_rejects_duplicate_project_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_file = Path(temp_dir) / "projects.json"
            registry = ProjectRegistry(projects_file)

            with self.assertRaises(ValueError) as exc_info:
                registry.save_projects(
                    [
                        ProjectDefinition(
                            project_id="alpha",
                            label="Alpha",
                            cwd=Path("/work/alpha"),
                            project_channel_id=111,
                            default_model_profile="gpt-5.4",
                            allowed_model_profiles=["gpt-5.4"],
                            active_session_id=None,
                            archived=False,
                        ),
                        ProjectDefinition(
                            project_id="alpha",
                            label="Beta",
                            cwd=Path("/work/beta"),
                            project_channel_id=222,
                            default_model_profile="gpt-5.4",
                            allowed_model_profiles=["gpt-5.4"],
                            active_session_id=None,
                            archived=False,
                        ),
                    ]
                )

        self.assertIn("Duplicate project_id", str(exc_info.exception))


if __name__ == "__main__":
    unittest.main()
