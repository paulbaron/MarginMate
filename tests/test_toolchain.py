"""The toolchain's files agree with one another (CLAUDE.md, « Toolchain »).

mise.toml pins the tools, pyproject.toml and uv.lock the Python packages,
prek.toml the git hooks; requirements.txt is the transitional export the
previous deploy.cmd installs from. Each test says what breaks when two of
them disagree.
"""

import re
import tomllib
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parent.parent


def toml(name):
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


def normalised(name):
    return re.sub(r"[-_.]+", "-", name).lower()


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

    def test_requirements_txt_pins_the_lock_s_versions_and_no_dev_tool(self):
        """The previous deploy.cmd runs pip on it once, on the new code: a pin
        other than the lock's would put another version in production."""
        locked = {normalised(p["name"]): p["version"] for p in toml("uv.lock")["package"]}
        pins = {}
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            line = line.split(";")[0].strip()
            if line and not line.startswith("#"):
                name, _, version = line.partition("==")
                pins[normalised(name)] = version.strip()
        self.assertTrue(pins)
        for name, version in pins.items():
            with self.subTest(package=name):
                self.assertEqual(locked.get(name), version)
        for tool in ("ruff", "ty", "django-stubs"):
            self.assertNotIn(tool, pins)
        for runtime in ("django", "waitress", "whitenoise", "rapidocr", "onnxruntime", "pyhanko"):
            self.assertIn(runtime, pins)

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
