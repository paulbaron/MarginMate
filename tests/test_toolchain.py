"""The toolchain's files agree with one another (CLAUDE.md, « Toolchain »).

mise.toml pins the tools, pyproject.toml and uv.lock the Python packages,
prek.toml the git hooks. Each test says what breaks when two of them
disagree.
"""

import tomllib
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent


def toml(name):
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


class ToolchainFilesTests(SimpleTestCase):
    def test_uv_is_asked_for_the_python_mise_installs(self):
        """Without .python-version, uv threw a pip-made .venv away to rebuild it
        on the first Python it found (mise's): production's, its OCR models
        included, would have gone at the next deploy."""
        asked = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
        self.assertEqual(asked, "3.11")
        self.assertEqual(toml("mise.toml")["tools"]["python"], asked)
        self.assertEqual(toml("pyproject.toml")["project"]["requires-python"], ">=3.11,<3.12")

    def test_mise_pins_uv_and_prek_exactly(self):
        tools = toml("mise.toml")["tools"]
        for tool in ("uv", "prek"):
            with self.subTest(tool=tool):
                self.assertRegex(tools[tool], r"^\d+\.\d+\.\d+$")

    def test_there_is_no_requirements_txt_and_no_hook_writes_one(self):
        """requirements.txt was the export of uv.lock the deploy.cmd of the
        versions before uv installed 05a80b4 from, with pip. Every deploy
        since installs with uv sync, so a copy back in the code is a second
        list of versions that nothing reads and nothing keeps in step. The
        hook that exported it went with it: left in, it wrote the file again
        at the next commit touching uv.lock."""
        self.assertFalse((ROOT / "requirements.txt").exists())
        hooks = [hook for repo in toml("prek.toml")["repos"] for hook in repo["hooks"]]
        self.assertTrue(hooks)
        self.assertEqual([hook["id"] for hook in hooks if "requirements" in str(hook)], [])

    def test_the_dev_tools_are_a_group_production_leaves_out(self):
        project = toml("pyproject.toml")
        runtime = " ".join(project["project"]["dependencies"])
        for tool in ("ruff", "ty", "django-stubs"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, runtime)
                self.assertTrue(any(d.startswith(tool) for d in project["dependency-groups"]["dev"]))
        self.assertIs(project["tool"]["uv"]["package"], False)

    def test_the_pre_push_hooks_are_the_ci(self):
        config = toml("prek.toml")
        self.assertIn("pre-push", config["default_install_hook_types"])
        pushed = {
            hook["id"]: hook["entry"]
            for repo in config["repos"]
            for hook in repo["hooks"]
            if "pre-push" in hook.get("stages", ())
        }
        for part in (
            "uv lock --check",
            "ruff check .",
            "ruff format --check .",
            "ty check",
            "manage.py check",
            "makemigrations --check",
            "manage.py test",
        ):
            with self.subTest(part=part):
                self.assertTrue(any(part in entry for entry in pushed.values()), part)
        tests = next(entry for entry in pushed.values() if "manage.py test" in entry)
        self.assertIn("--settings=config.settings_test", tests)
        self.assertIn("--exclude-tag=browser", tests)
