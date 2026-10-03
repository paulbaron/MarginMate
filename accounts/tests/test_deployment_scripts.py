"""deploy.cmd and refresh_dev_data.cmd (DEPLOY.md, section 10), their helper
accounts/deployment.py, and what DEPLOY.md and CLAUDE.md say about the two
copies (production in C:\\MarginMate, development here).

**The scripts are never run here.** deploy.cmd stops the owner's server
(the task « MarginMate », whatever listens on 127.0.0.1:8765) and changes
the production clone; refresh_dev_data.cmd moves a data folder aside. They
are read as text: the order of their steps, what each failure branch
reaches (`Script.reachable` follows the gotos and calls a branch can take),
and the cmd.exe traps they must stay clear of. The helper they call is
Python, tested as Python - and its one-liner, taken from the scripts
themselves, is run in a child process on temporary folders with the .env
never read (`run_the_scripts_python`).
"""

from __future__ import annotations

import base64
import contextlib
import io
import itertools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import warnings
from datetime import datetime
from pathlib import Path
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from accounts import data_backup, deployment, vault
from accounts.management.commands import serve
from accounts.models import PushDevice
from accounts.tests.test_production_settings import child_environment, deploy_md_lines
from invoices.scrapers import website

BASE = Path(settings.BASE_DIR)
#: What both scripts run to ask the settings (the helper's docstring).
SNIPPET = "import sys; from accounts import deployment; sys.exit(deployment.main())"
#: Windows PowerShell, as both scripts name it.
POWERSHELL = (
    Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
)
#: The keys deploy.cmd writes into etat.txt, by the variable each is read
#: back into. They stay French: the deploy.cmd of one version reads what
#: another wrote, and the owner is shown the file.
STATE_KEYS = {"PREVIOUS": "ancien", "DATA": "donnees", "BACKUP": "sauvegarde", "STEP": "etape"}
#: pip or its file, named anywhere: the way back installs with uv alone.
PIP = re.compile(r"\bpip\b|requirements\.txt", flags=re.IGNORECASE)
#: What every production uv sync asks for by --python: .python-version's.
PYTHON_VERSION = (BASE / ".python-version").read_text(encoding="utf-8").strip()
#: The command each of them is, in the scripts and in DEPLOY.md.
SYNC = f"uv sync --locked --no-dev --python {PYTHON_VERSION}"


class Script:
    """A .cmd file as cmd.exe reads it: lines, labels, sections."""

    def __init__(self, name: str):
        self.raw = (BASE / name).read_bytes()
        self.text = self.raw.decode("ascii", "replace")
        self.lines = [line.strip() for line in self.text.replace("\r\n", "\n").split("\n")]
        self.labels = {}
        for index, line in enumerate(self.lines):
            if line.startswith(":") and not line.startswith("::"):
                self.labels[line[1:].split()[0].lower()] = index

    @staticmethod
    def is_comment(line: str) -> bool:
        lowered = line.lower()
        return lowered == "rem" or lowered.startswith("rem ") or line.startswith("::")

    def commands(self, start=0, stop=None) -> list[tuple[int, str]]:
        """(index, line) of every command line - no blank, comment, label
        or « @echo off »."""
        stop = len(self.lines) if stop is None else stop
        return [
            (index, self.lines[index])
            for index in range(start, stop)
            if self.lines[index]
            and not self.is_comment(self.lines[index])
            and not self.lines[index].startswith(":")
            and self.lines[index].lower() != "@echo off"
        ]

    def executed(self, lines: list[str]) -> list[str]:
        """`lines` but what is only printed (echo)."""
        return [line for line in lines if not line.lower().startswith("echo")]

    def line_of(self, needle: str, start: int = 0) -> int:
        for index, line in self.commands(start):
            if needle in line:
                return index
        raise AssertionError(f"« {needle} » is not in the script")

    def after(self, index: int) -> str:
        """The command line that follows line `index`."""
        following = self.commands(index + 1)
        return following[0][1] if following else ""

    def section(self, label: str) -> list[str]:
        """The commands from `label` to the next label."""
        start = self.labels[label.lower().lstrip(":")]
        later = [index for index in self.labels.values() if index > start]
        stop = min(later) if later else len(self.lines)
        return [line for _, line in self.commands(start + 1, stop)]

    def _next_label(self, label: str) -> str | None:
        start = self.labels[label]
        later = sorted((index, name) for name, index in self.labels.items() if index > start)
        return later[0][1] if later else None

    def reachable(self, label: str) -> list[str]:
        """Every command a run reaching `label` can execute: its section,
        the sections its gotos and calls lead to, and the next section when
        it falls through (its last command is no goto or exit)."""
        seen, pending, lines = set(), [label.lower().lstrip(":")], []
        while pending:
            name = pending.pop()
            if name in seen or name == "eof":
                continue
            seen.add(name)
            section = self.section(name)
            lines += section
            for line in section:
                for target in re.findall(r"(?:goto|call)\s+:(\w+)", line, flags=re.IGNORECASE):
                    pending.append(target.lower())
            last = section[-1].lower() if section else ""
            if not last.startswith(("goto ", "exit")):
                following = self._next_label(name)
                if following:
                    pending.append(following)
        return lines

    def targets(self) -> set[str]:
        return {
            target.lower()
            for _, line in self.commands()
            for target in re.findall(r"(?:goto|call)\s+:(\w+)", line, flags=re.IGNORECASE)
        }


def unquoted(line: str) -> str:
    return re.sub(r'"[^"]*"', '""', line)


def task_check_command(script: Script) -> str:
    """The PowerShell deploy.cmd runs to read the task's action: what lies
    between -Command's quotes, cmd's variables still unexpanded."""
    line = script.lines[script.line_of("Get-ScheduledTask")]
    return line.split(' -Command "', 1)[1].rsplit('"', 1)[0]


def make_junction(test, link: Path, target: Path) -> Path:
    """`link`, a directory junction to `target`, made by « mklink /J » as
    the owner would make one - between two temporary folders only, never
    towards the code or C:\\MarginMate. Removed at the test's end, the
    junction alone (os.rmdir never goes into it), before the temporary
    folder is. The test is skipped where mklink makes none."""
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not link.is_dir():
        test.skipTest(f"mklink /J n'a pas fait de jonction (code {result.returncode})")
    test.addCleanup(os.rmdir, link)
    return link


def short_name(path: Path) -> str | None:
    """The 8.3 short name Windows gives `path` (an existing one), None where
    the volume makes none - or where it is the long name itself."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    get = ctypes.windll.kernel32.GetShortPathNameW
    get.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(1024)
    length = get(str(path), buffer, len(buffer))
    if not length or length >= len(buffer):
        return None
    short = buffer.value
    return short if os.path.normcase(short) != os.path.normcase(str(path)) else None


class CmdHygieneMixin:
    """cmd.exe's traps, for any script of the repository."""

    name = ""

    def setUp(self):
        super().setUp()
        self.script = Script(self.name)

    def test_ascii_with_crlf_line_ends(self):
        """cmd.exe reads a batch file in the console's code page (ASCII only,
        as start_production.cmd), and with LF alone « goto :label » and
        « call :label » can miss a label that straddles one of its 512-byte
        reads (.gitattributes keeps CRLF on every checkout)."""
        self.assertTrue(self.script.raw.isascii())
        self.assertEqual(self.script.raw.count(b"\n"), self.script.raw.count(b"\r\n"))
        self.assertTrue(self.script.raw.endswith(b"\r\n"))
        attributes = (BASE / ".gitattributes").read_text(encoding="utf-8").splitlines()
        self.assertIn("*.cmd text eol=crlf", attributes)

    def test_every_goto_and_call_has_its_label(self):
        missing = self.script.targets() - set(self.script.labels) - {"eof"}
        self.assertEqual(missing, set())

    def test_no_label_is_defined_twice(self):
        """cmd.exe goes to the first one it meets after the current line,
        wrapping round the end of the file: a second definition of a label
        is a goto that lands on either, depending on where it is taken."""
        names = [
            line[1:].split()[0].lower()
            for line in self.script.lines
            if line.startswith(":") and not line.startswith("::")
        ]
        self.assertEqual(sorted(name for name in set(names) if names.count(name) > 1), [])

    def test_every_variable_is_set_before_it_is_read(self):
        """A misspelt %MM_...% expands to nothing, silently: a rollback line
        printed without its folder, a mark never removed. Every variable a
        line reads (%MM_X%, %MM_X:...%, « defined MM_X ») is set by a line
        above it - in the file's order, which is the order the main path
        runs in; the branches and subroutines below only read. An answer of
        the helper (for /f ... set "MM_%%a=%%b") is set to empty first: one
        the helper did not give must read as empty, never as a value the
        console that started the script happened to hold. MM_NOTE_<key> are
        etat.txt's keys, read by the for /f that makes them."""
        first_set = {}
        notes_from = None
        for index, line in self.script.commands():
            for name in re.findall(r'set "(MM_\w+)=', line):
                first_set.setdefault(name.upper(), index)
            if 'set "MM_NOTE_%%a=%%b"' in line and notes_from is None:
                notes_from = index
        for index, line in self.script.commands():
            read = re.findall(r"%(MM_\w+?)[%:]", line) + re.findall(r"\bdefined (MM_\w+)", line, flags=re.IGNORECASE)
            for name in read:
                with self.subTest(variable=name, line=line):
                    if name.upper().startswith("MM_NOTE_"):
                        self.assertIsNotNone(notes_from)
                        self.assertLess(notes_from, index)
                    else:
                        self.assertIn(name.upper(), first_set)
                        self.assertLessEqual(first_set[name.upper()], index)

    def test_no_delayed_expansion_and_no_bang(self):
        """With delayed expansion on, a « ! » in a path or a sentence
        vanishes; nothing here needs it (every variable set and read in one
        parenthesised block is avoided)."""
        self.assertIn("setlocal EnableExtensions DisableDelayedExpansion", self.script.text)
        self.assertNotIn("EnableDelayedExpansion", self.script.text)
        for _, line in self.script.commands():
            self.assertNotIn("!", line)

    def test_no_percent_in_a_comment(self):
        """cmd expands %...% in a rem line too, and a bad « %~ » there stops
        the script."""
        for line in self.script.lines:
            if Script.is_comment(line):
                self.assertNotIn("%", line, line)

    def test_errorlevel_is_never_read_after_a_pipe(self):
        """After « a | b », ERRORLEVEL is b's: a failure of a is lost."""
        commands = self.script.commands()
        for (_, line), (_, following) in itertools.pairwise(commands):
            if following.lower().startswith("if errorlevel") or "%ERRORLEVEL%" in following:
                self.assertNotIn("|", unquoted(line), line)

    def test_utf_8_output(self):
        """git's and Python's accented output: the console in UTF-8, and
        Python writing UTF-8 even into a file or a pipe."""
        text = self.script.text
        self.assertIn('set "PYTHONIOENCODING=utf-8"', text)
        first_python = self.script.line_of('"%MM_PYTHON%"')
        self.assertLess(self.script.line_of("chcp 65001 >nul"), first_python)
        self.assertLess(self.script.line_of('set "PYTHONIOENCODING=utf-8"'), first_python)

    def test_the_project_s_python_quoted(self):
        self.assertIn('set "MM_PYTHON=.venv\\Scripts\\python.exe"', self.script.text)
        for _, line in self.script.commands():
            # uv's --python names a version, it runs no Python of its own.
            if "python" in line.lower() and not line.lower().startswith(("set ", "echo", "if ", "call uv ")):
                self.assertTrue(line.startswith('"%MM_PYTHON%"') or "MM_PYTHON" in line, line)

    def test_the_settings_are_asked_through_the_helper(self):
        """Never the .env read by hand (findstr on DJANGO_DEBUG): the helper
        loads the settings as every command does."""
        self.assertIn(f'"%MM_PYTHON%" -c "{SNIPPET}"', self.script.text)
        for _, line in self.script.commands():
            self.assertFalse("findstr" in line.lower() and ".env" in line.lower(), line)

    def test_the_window_stays_open_at_the_end(self):
        """Run from Explorer, the window closes with the script: every end
        goes through a pause first."""
        end = self.script.section("finish")
        self.assertIn("pause", end)
        self.assertTrue(end[-1].startswith("exit /b"))


class DeployScriptTests(CmdHygieneMixin, SimpleTestCase):
    name = "deploy.cmd"

    def test_it_runs_from_a_copy_of_itself(self):
        """git rewrites deploy.cmd when the change being deployed touches
        it, while cmd.exe goes on reading it line by line: the rest of the
        run would come from the new file at the old offset. It copies itself
        to %TEMP% first and hands over WITHOUT call (a call would come back
        to the original, rewritten, file)."""
        commands = self.script.commands()
        self.assertEqual(commands[0][1], "setlocal EnableExtensions DisableDelayedExpansion")
        self.assertEqual(commands[1][1], 'if /i "%~1"=="--from-the-copy" goto :from_the_copy')
        copy = self.script.line_of('copy /y "%~f0" "%MM_COPY%"')
        handover = self.script.line_of('"%MM_COPY%" --from-the-copy "%~dp0"')
        self.assertLess(copy, handover)
        self.assertFalse(self.script.lines[handover].lower().startswith("call"))
        self.assertIn('set "MM_COPY=%TEMP%\\', self.script.text)
        # From the copy, the folder is the one handed over: never %~dp0.
        start = self.script.labels["from_the_copy"]
        self.assertEqual(self.script.after(start), 'set "MM_APP=%~2"')
        into = self.script.line_of('cd /d "%MM_APP%"')
        self.assertEqual(self.script.after(into), "if errorlevel 1 goto :folder_not_found")
        self.assertLess(into, self.script.line_of('"%MM_PYTHON%"'))
        self.assertLess(self.script.line_of('set "MM_CODE=1"', start), into)
        self.assertNotIn("%~dp0", " ".join(line for _, line in self.script.commands(start)))
        # Nothing touches git, the server or the data before the handover.
        for _, line in self.script.commands(0, handover):
            for danger in ("git ", "schtasks", "Stop-Process", "manage.py"):
                self.assertNotIn(danger, line)

    def test_it_refuses_to_run_anywhere_but_production(self):
        """The development folder's .env says DJANGO_DEBUG=True and no
        MARGINMATE_HTTPS: there, deploy.cmd would stop, back up and migrate
        the development copy. Asked before anything else is done."""
        check = self.script.line_of(f'"%MM_PYTHON%" -c "{SNIPPET}" production')
        self.assertEqual(self.script.after(check), "if errorlevel 1 goto :not_production")
        self.assertLess(check, self.script.line_of("git fetch origin"))
        refusal = self.script.reachable("not_production")
        for danger in ("schtasks", "Stop-Process", "git ", "manage.py", "robocopy"):
            self.assertFalse([line for line in self.script.executed(refusal) if danger in line], danger)

    # -- uv ---------------------------------------------------------------------------------------------------------

    def test_it_refuses_when_uv_does_not_answer(self):
        """Step 7 installs with uv: on a PC where uv does not answer, the
        run would stop the server, back up, merge, and fail there with the
        site down. Asked first, by RUNNING it - a mise shim on the PATH is
        there even when mise is not (« mise-shim: failed to execute mise »,
        exit 1) - and through call, as git: a shim may be a batch file,
        which would not hand control back without it."""
        check = self.script.line_of("uv --version")
        self.assertEqual(self.script.lines[check], "call uv --version >nul")
        self.assertEqual(self.script.after(check), "if errorlevel 1 goto :no_uv")
        self.assertNotIn("where uv", self.script.text)
        # With the other refusals at the top, after the folder's checks...
        self.assertLess(self.script.line_of("if errorlevel 1 goto :no_git"), check)
        # ... before the mark, and before anything is asked or done.
        self.assertLess(check, self.script.line_of(self.LOCK))
        self.assertLess(self.script.line_of('set "MM_RELEASE=0"'), check)
        for step in (
            f'"%MM_PYTHON%" -c "{SNIPPET}" production',
            "git rev-parse",
            "git fetch",
            "manage.py",
            "schtasks",
            "Stop-Process",
        ):
            self.assertLess(check, self.script.line_of(step), step)
        refused = self.script.reachable("no_uv")
        self.assertNotIn('set "MM_RELEASE=1"', refused)
        for danger in ("schtasks", "Stop-Process", "git ", "manage.py", "robocopy", "move ", "mkdir", "uv sync"):
            self.assertFalse([line for line in self.script.executed(refused) if danger in line], danger)
        said = " ".join(self.script.section("no_uv"))
        for words in (
            "REFUS : uv ne repond pas",
            "DEPLOY.md",
            "section 10.6",
            "mise install",
            "uv --version",
            "NOUVELLE invite de commandes",
            "Rien n'a ete fait",
        ):
            self.assertIn(words, said)
        # The section it points at.
        self.assertIn("\n### 10.6 Passage à uv (une seule fois)\n", (BASE / "DEPLOY.md").read_text(encoding="utf-8"))

    def test_uv_silent_with_a_mark_left_says_the_way_back_first(self):
        """A mark is a deployment that failed past its merge, or a window
        closed half way: the site may be down, and the way back comes first,
        whatever uv answers."""
        self.assertEqual(self.script.section("no_uv")[0], 'if exist "%MM_LOCK%\\" goto :already_running')

    def test_step_7_installs_what_uv_lock_says(self):
        """--locked: uv.lock exactly, refused when it disagrees with
        pyproject.toml; --no-dev: none of the development tools. After the
        merge and before the migrations, and a failure there is a failure
        past the merge. uv is only ever run through call."""
        sync = self.script.line_of("call uv sync")
        self.assertEqual(self.script.lines[sync], f"call uv sync --locked --no-dev --python {PYTHON_VERSION}")
        self.assertEqual(self.script.after(sync), "if errorlevel 1 goto :failure_after_merge")
        self.assertLess(self.script.line_of("call git merge --ff-only origin/main"), sync)
        self.assertLess(sync, self.script.line_of('"%MM_PYTHON%" manage.py migrate_tenants'))
        commands = [line for _, line in self.script.commands()]
        self.assertIn('set "MM_STEP=l\'installation des dependances (uv sync --locked --no-dev)"', commands)
        self.assertIn("echo Dependances (uv sync --locked --no-dev)...", commands)
        self.assertEqual(
            [line for line in commands if re.match(r"(call\s+)?uv\b", line, flags=re.IGNORECASE)],
            ["call uv --version >nul", f"call uv sync --locked --no-dev --python {PYTHON_VERSION}"],
        )

    def test_every_uv_sync_names_the_python_of_python_version(self):
        """Run or printed, every uv sync says --python, the version
        .python-version names: an explicit request beats whatever mise hands
        uv, and the mise.toml of every version up to 01/10/2026 handed it the
        exact Python mise installed (python.uv_venv_auto) - uv then replaced
        a .venv made on another 3.11, OCR models included. The way back puts
        such a mise.toml back on disk before its uv sync runs."""
        # The step's own name (« l'installation des dependances (uv sync
        # --locked --no-dev) ») is words in etat.txt and on screen, no command.
        syncs = [
            line
            for _, line in self.script.commands()
            if re.search(r"\buv sync\b", line) and not line.startswith(("set ", "echo Dependances"))
        ]
        # Step 7 runs it; « offline » and the way back print it.
        self.assertEqual(syncs, [f"call {SYNC}", f"echo   {SYNC}", f"echo   {SYNC}"])

    def test_pip_is_neither_run_nor_named(self):
        """uv sync takes pip out of .venv, and nothing here runs it - nor
        names it: the way back installs with uv, and a version from before
        uv is not gone back to (the owner, 01/10/2026). Its requirements.txt
        is gone from the code, and its deploy.cmd, back in place, would fail
        the next deployment on it."""
        self.assertEqual([line for line in self.script.lines if PIP.search(line)], [])
        for label in ("offline", "rollback_instructions"):
            with self.subTest(label=label):
                self.assertIn(f"echo   uv sync --locked --no-dev --python {PYTHON_VERSION}", self.script.section(label))

    def test_the_wait_is_for_free_or_listening_only(self):
        """:wait_for_port compares its second argument with « listening »:
        anything else, a typo included, would silently wait for the port to
        be FREE."""
        waits = [line for _, line in self.script.commands() if ":wait_for_port" in line and line.startswith("call")]
        self.assertTrue(waits)
        for line in waits:
            with self.subTest(line=line):
                self.assertRegex(line, r"^call :wait_for_port %MM_PORT% (free|listening) [0-9]+$")
        self.assertIn("('%2' -eq 'listening')", " ".join(self.script.section("wait_for_port")))

    def test_etat_txt_keeps_the_keys_every_version_reads(self):
        """A mark left by one version's deploy.cmd is read by the next
        one's: etat.txt's keys are an interface between versions and never
        follow a rename of the variables."""
        written = set()
        for _, line in self.script.commands():
            if line.startswith(('>"%MM_STATE%"', '>>"%MM_STATE%"')):
                match = re.fullmatch(r'>>?"%MM_STATE%" echo (\w+)=%MM_(\w+)%', line)
                self.assertIsNotNone(match, line)
                written.add((match.group(1), match.group(2)))
        self.assertEqual(written, {(key, variable) for variable, key in STATE_KEYS.items()})

    def test_the_steps_in_their_order(self):
        steps = [
            f'"%MM_PYTHON%" -c "{SNIPPET}" production',
            "call git diff --quiet HEAD --",
            "call git fetch origin",
            "git rev-parse HEAD",
            "call git --no-pager log --oneline HEAD..origin/main",
            'choice /C ON /N /M "Deployer ces changements ? (O/N) "',
            '"%MM_PYTHON%" manage.py running_jobs',
            'schtasks /end /tn "%MM_TASK%" >nul 2>&1',
            "Stop-Process",
            "call :wait_for_port %MM_PORT% free",
            '"%MM_PYTHON%" manage.py backup_data --chemin-dans "%MM_BACKUP_PATH_FILE%"',
            "call git merge --ff-only origin/main",
            "call uv sync --locked --no-dev",
            '"%MM_PYTHON%" manage.py migrate_tenants',
            '"%MM_PYTHON%" manage.py serve --verifier',
            "call :start_server",
            "call :wait_for_port %MM_PORT% listening 120",
            "Deploye : %MM_PREVIOUS_SHORT%..%MM_NEW_SHORT%",
        ]
        where = [self.script.line_of(step) for step in steps]
        self.assertEqual(where, sorted(where), list(zip(steps, where)))
        # The success path ends there, before any failure branch.
        self.assertLess(where[-1], min(self.script.labels[name] for name in ("backup_failure", "failure_after_merge")))

    def test_each_step_s_failure_goes_where_it_should(self):
        expected = {
            "call uv --version >nul": "if errorlevel 1 goto :no_uv",
            "call git fetch origin": "if errorlevel 1 goto :fetch_failed",
            '"%MM_PYTHON%" manage.py running_jobs': "if errorlevel 1 goto :jobs_running",
            "call :wait_for_port %MM_PORT% free 30": "if errorlevel 1 goto :cannot_stop",
            '"%MM_PYTHON%" manage.py backup_data': "if errorlevel 1 goto :backup_failure",
            "call :wait_for_port %MM_PORT% free 0": "if errorlevel 1 goto :server_came_back",
            "call git merge --ff-only origin/main": "if errorlevel 1 goto :merge_failure",
            "call uv sync --locked --no-dev": "if errorlevel 1 goto :failure_after_merge",
            '"%MM_PYTHON%" manage.py migrate_tenants': "if errorlevel 1 goto :failure_after_merge",
            '"%MM_PYTHON%" manage.py serve --verifier': "if errorlevel 1 goto :failure_after_merge",
            "call :wait_for_port %MM_PORT% listening 120": "if errorlevel 1 goto :server_silent",
        }
        for step, then in expected.items():
            with self.subTest(step=step):
                self.assertEqual(self.script.after(self.script.line_of(step)), then)

    def test_nothing_new_says_so_and_changes_nothing(self):
        self.assertIn('if "%MM_NEW_COMMITS%"=="0" goto :nothing_new', self.script.text)
        branch = self.script.reachable("nothing_new")
        self.assertTrue(any("Rien de nouveau" in line for line in branch))
        self.assertIn('set "MM_CODE=0"', branch)
        for danger in ("schtasks", "Stop-Process", "merge", "manage.py"):
            self.assertFalse([line for line in self.script.executed(branch) if danger in line], danger)

    def test_it_asks_before_doing_anything(self):
        """« choice » answers 1 for O, 2 for N, 0 on Ctrl+C and 255 on an
        error: only 1 goes on."""
        ask = self.script.line_of('choice /C ON /N /M "Deployer ces changements ? (O/N) "')
        self.assertEqual(self.script.after(ask), 'if not "%ERRORLEVEL%"=="1" goto :cancelled')
        self.assertLess(ask, self.script.line_of("manage.py running_jobs"))
        cancelled = self.script.reachable("cancelled")
        for danger in ("schtasks", "Stop-Process", "merge", "manage.py"):
            self.assertFalse([line for line in self.script.executed(cancelled) if danger in line], danger)

    def test_a_running_job_stops_it_before_the_server_is_stopped(self):
        jobs = self.script.line_of("manage.py running_jobs")
        self.assertLess(jobs, self.script.line_of("schtasks /end"))
        self.assertLess(jobs, self.script.line_of("Stop-Process"))
        refusal = self.script.reachable("jobs_running")
        self.assertTrue(any("attendez" in line for line in refusal))
        for danger in ("schtasks", "Stop-Process", "merge", "backup_data"):
            self.assertFalse([line for line in self.script.executed(refusal) if danger in line], danger)

    def test_the_server_is_stopped_on_8765_never_8000(self):
        """8765 is serve's (accounts/management/commands/serve.py); 8000 is
        runserver's, the development server's, which deploy.cmd never
        touches."""
        self.assertIn(f'set "MM_PORT={serve.DEFAULT_PORT}"', self.script.text)
        self.assertNotIn("8000", self.script.text)
        stop = self.script.lines[self.script.line_of("Stop-Process")]
        self.assertIn("Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %MM_PORT% -State Listen", stop)
        self.assertIn("Stop-Process -Id $_.OwningProcess -Force", stop)
        self.assertIn('set "MM_TASK=MarginMate"', self.script.text)
        # Whether it listened before, for a failure that restarts it « as it was ».
        was = self.script.line_of("call :wait_for_port %MM_PORT% listening 0")
        self.assertEqual(self.script.after(was), 'if not errorlevel 1 set "MM_WAS_RUNNING=1"')
        self.assertLess(was, self.script.line_of("schtasks /end"))
        # The wait: PowerShell's state, not netstat's words (translated).
        wait = self.script.section("wait_for_port")
        self.assertTrue(
            any("Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort %1 -State Listen" in line for line in wait)
        )
        self.assertNotIn("netstat", self.script.text.lower())

    def test_the_previous_commit_is_recorded_before_the_merge(self):
        recorded = self.script.line_of('for /f "delims=" %%h in (\'git rev-parse HEAD\') do set "MM_PREVIOUS=%%h"')
        self.assertLess(recorded, self.script.line_of("git merge --ff-only"))
        self.assertLess(self.script.line_of("backup_data"), self.script.line_of("git merge --ff-only"))
        self.assertNotIn("git pull", self.script.text)
        self.assertNotIn("reset --hard origin", self.script.text)

    def test_a_failed_backup_restarts_the_server_as_it_was(self):
        branch = self.script.section("backup_failure")
        self.assertIn("call :restart_if_running", branch)
        self.assertFalse(
            [line for line in self.script.executed(self.script.reachable("backup_failure")) if "merge" in line]
        )
        restart = self.script.section("restart_if_running")
        self.assertEqual(restart[0], 'if "%MM_WAS_RUNNING%"=="1" goto :restart')

    def test_a_failed_merge_goes_back_to_the_old_code(self):
        branch = self.script.section("merge_failure")
        reset = branch.index("call git reset --hard %MM_PREVIOUS%")
        self.assertEqual(branch[reset + 1], "if errorlevel 1 goto :failure_after_merge")
        self.assertIn("call :restart_if_running", branch[reset:])

    def test_nothing_restarts_the_server_after_a_failed_step(self):
        """Past the merge, a failure leaves the site OFF: restarted, the new
        code would run on half-migrated data, or the old code on migrated
        data. The branch says how to go back instead."""
        for label in ("failure_after_merge", "server_silent"):
            with self.subTest(label=label):
                branch = self.script.executed(self.script.reachable(label))
                for restart in ("call :start_server", "call :restart", "schtasks /run", 'start "', "start_production"):
                    self.assertFalse([line for line in branch if restart in line], restart)
        said = " ".join(self.script.reachable("failure_after_merge"))
        for command in (
            "git reset --hard %MM_PREVIOUS%",
            "uv sync --locked --no-dev",
            'move "%MM_DATA%" "%MM_DATA%.echec"',
            'robocopy "%MM_BACKUP%\\data" "%MM_DATA%" /E',
            "schtasks /run /tn MarginMate",
        ):
            self.assertIn(command, said)
        self.assertIn("N'A PAS ete relance", said)

    def test_the_server_is_checked_still_stopped_before_the_merge(self):
        """A server started again meanwhile (a task restarting itself) would
        run the old code while the new one migrates its data."""
        self.assertLess(self.script.line_of("backup_data"), self.script.line_of("call :wait_for_port %MM_PORT% free 0"))
        self.assertLess(
            self.script.line_of("call :wait_for_port %MM_PORT% free 0"), self.script.line_of("merge --ff-only")
        )

    def test_the_restart_uses_the_task_when_there_is_one(self):
        start = self.script.section("start_server")
        self.assertEqual(start[0], 'schtasks /query /tn "%MM_TASK%" >nul 2>&1')
        self.assertIn('schtasks /run /tn "%MM_TASK%" >nul', start)
        window = self.script.section("start_in_a_window")
        self.assertIn('start "MarginMate" cmd /k start_production.cmd', window)

    def test_the_production_clone_must_be_clean_and_on_main(self):
        """A file edited in C:\\MarginMate\\app by hand would be lost to
        « git reset --hard » on the way back, or stop the merge half way."""
        clean = self.script.line_of("call git diff --quiet HEAD --")
        following = [line for _, line in self.script.commands(clean + 1)[:2]]
        self.assertEqual(following, ["if errorlevel 2 goto :git_unreadable", "if errorlevel 1 goto :code_modified"])
        self.assertLess(clean, self.script.line_of("git fetch origin"))
        branch = self.script.line_of("git rev-parse --abbrev-ref HEAD")
        following = [line for _, line in self.script.commands(branch + 1)[:3]]
        self.assertEqual(following[0], "if errorlevel 1 goto :git_unreadable")
        self.assertEqual(following[2], 'if /i not "%MM_BRANCH%"=="main" goto :not_on_main')
        ancestor = self.script.line_of("call git merge-base --is-ancestor HEAD origin/main")
        self.assertEqual(self.script.after(ancestor), "if errorlevel 1 goto :diverged_histories")
        self.assertLess(ancestor, self.script.line_of("choice /C ON"))

    def test_a_git_error_is_said_as_such(self):
        """C:\\MarginMate made from an administrator's prompt belongs to
        BUILTIN\\Administrators, and git run from a plain double-click
        answers « detected dubious ownership »: the branch read inside a
        for /f came out empty (« pas sur la branche main, elle est sur "" »),
        and git diff's 128 read as « des fichiers ont ete modifies »."""
        branch = self.script.lines[self.script.line_of("git rev-parse --abbrev-ref HEAD")]
        self.assertEqual(branch, 'call git rev-parse --abbrev-ref HEAD > "%MM_ANSWERS%.branch"')
        self.assertNotIn("('git rev-parse --abbrev-ref HEAD')", self.script.text)
        read = self.script.after(self.script.line_of("if errorlevel 1 goto :git_unreadable"))
        self.assertEqual(read, 'for /f "usebackq delims=" %%b in ("%MM_ANSWERS%.branch") do set "MM_BRANCH=%%b"')
        said = " ".join(self.script.reachable("git_unreadable"))
        self.assertIn("REFUS : git ne lit pas ce dossier (message ci-dessus)", said)
        self.assertIn("dubious ownership", said)
        self.assertIn("git config --global --add safe.directory", said)
        self.assertIn("Rien n'a ete fait", said)
        for danger in ("schtasks", "Stop-Process", "merge", "manage.py", "reset"):
            self.assertFalse(
                [line for line in self.script.executed(self.script.reachable("git_unreadable")) if danger in line],
                danger,
            )
        self.assertTrue(
            any(".branch" in line and line.startswith(("del ", "if defined")) for line in self.script.section("finish"))
        )

    def test_git_never_opens_a_pager(self):
        for _, line in self.script.commands():
            if " log " in f" {line} " and "git" in line:
                self.assertIn("--no-pager", line)

    # -- One deployment at a time, and one left half way -----------------------------------------------------------

    LOCK = 'mkdir "%MM_LOCK%" 2>nul || goto :already_running'

    def test_one_deploy_at_a_time(self):
        """Two windows (a double-click that seemed to do nothing, a relaunch
        while the quiet pip of the time looked hung): the second's failed merge ran « git reset
        --hard » under the first, which then restarted the OLD code on
        migrated data and printed « Deploye ». A folder made with mkdir -
        which fails when it exists - is taken before step 1, by one window
        only."""
        self.assertIn('set "MM_LOCK=%MM_APP%.git\\marginmate-deploy"', self.script.text)
        lock = self.script.line_of(self.LOCK)
        self.assertEqual(self.script.after(lock), 'set "MM_RELEASE=1"')
        # After the checks that it is a clone (mkdir would make a missing .git)...
        self.assertLess(self.script.line_of('if not exist ".git\\" goto :not_a_clone'), lock)
        self.assertLess(self.script.line_of("if errorlevel 1 goto :no_git"), lock)
        # ... and before anything is asked or done.
        self.assertLess(lock, self.script.line_of(f'"%MM_PYTHON%" -c "{SNIPPET}" production'))
        for step in ("git fetch origin", "manage.py running_jobs", "schtasks /end", "git merge --ff-only"):
            self.assertLess(lock, self.script.line_of(step), step)
        # Never a handle held open (9>"file"): « start » would hand it to the
        # server's window, which would hold it for its whole life.
        self.assertIsNone(re.search(r'\d>\s*"[^"]*(?:lock|marginmate-deploy)', self.script.text, flags=re.IGNORECASE))
        self.assertEqual(self.script.text.count('set "MM_RELEASE=1"'), 1)
        # Set to 0 before anything can go to :finish.
        self.assertLess(self.script.line_of('set "MM_RELEASE=0"'), self.script.line_of("goto :no_venv"))

    def test_the_mark_goes_only_with_the_run_that_made_it(self):
        """:finish removes the folder when THIS window made it: a refused
        second window must never remove the first one's."""
        end = self.script.section("finish")
        self.assertEqual(end[0], 'if "%MM_RELEASE%"=="1" rmdir /s /q "%MM_LOCK%" 2>nul')
        removals = [
            line
            for _, line in self.script.commands()
            if re.search(r"(^|\s)(rd|rmdir)\s", line) and not line.startswith("echo")
        ]
        self.assertEqual(removals, [end[0]])
        refused = self.script.reachable("already_running")
        self.assertNotIn('set "MM_RELEASE=1"', refused)
        for danger in ("schtasks", "Stop-Process", "git merge", "git reset", "manage.py", "robocopy", "move "):
            self.assertFalse([line for line in self.script.executed(refused) if danger in line], danger)

    def test_a_failure_past_the_merge_keeps_the_mark(self):
        """Server off, HEAD already the new commit: the next double-click
        must not say « Rien de nouveau … Le serveur n'a pas ete touche » and
        exit 0 while the site is down."""
        for label in ("failure_after_merge", "server_silent"):
            with self.subTest(label=label):
                self.assertEqual(self.script.section(label)[0], 'set "MM_RELEASE=0"')
        # Every other ending lets it go: success and the failures before the
        # merge, after their restart (a merge undone by git reset included;
        # a reset that fails is a failure past the merge).
        for label in ("backup_failure", "job_cut_off", "server_came_back", "cannot_stop", "task_elsewhere"):
            with self.subTest(label=label):
                self.assertNotIn('set "MM_RELEASE=0"', self.script.reachable(label))
        self.assertNotIn('set "MM_RELEASE=0"', self.script.section("merge_failure"))
        success = self.script.line_of("Deploye : %MM_PREVIOUS_SHORT%..%MM_NEW_SHORT%")
        self.assertNotIn(
            'set "MM_RELEASE=0"',
            [line for _, line in self.script.commands(self.script.line_of("merge --ff-only"), success)],
        )

    def test_where_it_stands_is_written_down(self):
        """etat.txt, in the mark's folder: the commit to go back to and the
        data folder just before the stop, the backup once made, and every
        step as it starts - what a closed window leaves for the next one."""
        self.assertIn('set "MM_STATE=%MM_APP%.git\\marginmate-deploy\\etat.txt"', self.script.text)
        first = self.script.line_of('>"%MM_STATE%" echo ancien=%MM_PREVIOUS%')
        self.assertLess(self.script.line_of("for /f \"delims=\" %%h in ('git rev-parse HEAD')"), first)
        self.assertLess(self.script.line_of("manage.py running_jobs"), first)
        self.assertLess(self.script.line_of("Get-ScheduledTask"), first)
        self.assertLess(first, self.script.line_of("schtasks /end"))
        self.assertLess(first, self.script.line_of('>>"%MM_STATE%" echo donnees=%MM_DATA%'))
        saved = self.script.line_of('>>"%MM_STATE%" echo sauvegarde=%MM_BACKUP%')
        self.assertLess(self.script.line_of('set "MM_BACKUP=%%s"'), saved)
        self.assertLess(saved, self.script.line_of("call git merge --ff-only origin/main"))
        # Each step, as it starts.
        steps = [
            index
            for index, line in self.script.commands()
            if line.startswith('set "MM_STEP=') and "MM_NOTE_" not in line
        ]
        self.assertEqual(self.script.lines[steps[0]], 'set "MM_STEP=la preparation"')
        for index in steps[1:]:
            with self.subTest(step=self.script.lines[index]):
                self.assertEqual(self.script.after(index), '>>"%MM_STATE%" echo etape=%MM_STEP%')
        for step in (
            "backup_data",
            "git merge --ff-only",
            "uv sync --locked --no-dev",
            "migrate_tenants",
            "serve --verifier",
            "relance",
        ):
            with self.subTest(step=step):
                self.assertTrue(any(step in self.script.lines[index] for index in steps), step)
        # Written with the redirection first: « echo ancien=…3>>file » would
        # take a commit's last digit for a handle.
        for _, line in self.script.commands():
            if "%MM_STATE%" in line and ">" in unquoted(line):
                self.assertTrue(line.startswith(('>"%MM_STATE%" echo ', '>>"%MM_STATE%" echo ')), line)

    def test_a_mark_left_behind_is_refused_with_the_way_back(self):
        self.assertEqual(self.script.section("already_running")[0], 'if not exist "%MM_LOCK%\\" goto :mark_failed')
        refused = self.script.reachable("already_running")
        said = " ".join(refused)
        self.assertIn("REFUS", said)
        self.assertIn('type "%MM_STATE%"', refused)
        self.assertIn('for /f "usebackq tokens=1,* delims==" %%a in ("%MM_STATE%") do set "MM_NOTE_%%a=%%b"', refused)
        # Each variable from its etat.txt key: the keys stay French, the
        # variables are this version's own.
        for variable, key in STATE_KEYS.items():
            self.assertIn(f'set "MM_{variable}=%MM_NOTE_{key.upper()}%"', refused)
        self.assertIn("goto :rollback_instructions", refused)
        self.assertIn("autre fenetre deploy.cmd", said)
        # Nothing noted: it was stopped before the server was.
        self.assertIn('if not exist "%MM_STATE%" goto :already_running_without_state', refused)
        self.assertIn('rmdir /s /q "%MM_LOCK%"', " ".join(self.script.section("already_running_without_state")))
        # The way back says to take the mark away, then to deploy again.
        back = " ".join(self.script.reachable("rollback_instructions"))
        self.assertIn('rmdir /s /q "%MM_LOCK%"', back)
        self.assertIn("Une fois revenu a la version d'avant (git reset ci-dessus), relancez deploy.cmd", back)
        self.assertNotIn("Pour reessayer la mise en ligne", back)
        # Without a backup noted, no restore is offered from an empty path.
        self.assertIn(
            "if not defined MM_BACKUP goto :rollback_without_backup", self.script.section("rollback_instructions")
        )

    def test_nothing_new_with_the_site_down_says_so(self):
        """HEAD already origin/main and nothing listening on 8765: after a
        failure past the merge whose mark was removed by hand, or a server
        that never came back. Never « Le serveur n'a pas ete touche » and 0."""
        branch = self.script.section("nothing_new")
        self.assertEqual(branch[:2], ["call :wait_for_port %MM_PORT% listening 0", "if errorlevel 1 goto :offline"])
        offline = self.script.reachable("offline")
        said = " ".join(offline)
        self.assertIn("ATTENTION : rien n'ecoute sur 127.0.0.1:%MM_PORT%, le site est hors ligne", said)
        self.assertIn("schtasks /run /tn MarginMate", said)
        self.assertIn("git reset --hard", said)
        self.assertIn("manage.py serve --verifier", said)
        self.assertNotIn('set "MM_CODE=0"', offline)
        for danger in ("schtasks", "Stop-Process", "merge", "manage.py", "reset"):
            self.assertFalse([line for line in self.script.executed(offline) if danger in line], danger)

    # -- The task, and a job started under the stop -----------------------------------------------------------------

    def test_the_task_must_start_this_folder(self):
        """The task created before the split still started the DEVELOPMENT
        folder's start_production.cmd: the restart served the wrong code,
        and « Deploye » was printed if anything listened on 8765."""
        check = self.script.line_of("Get-ScheduledTask")
        self.assertEqual(self.script.after(check), "if errorlevel 1 goto :task_elsewhere")
        self.assertLess(self.script.line_of("manage.py running_jobs"), check)
        for later in ("call :wait_for_port %MM_PORT% listening 0", "schtasks /end", "Stop-Process", "backup_data"):
            self.assertLess(check, self.script.line_of(later), later)
        line = self.script.lines[check]
        self.assertTrue(line.startswith('"%MM_POWERSHELL%" -NoProfile -NonInteractive -Command "'))
        command = task_check_command(self.script)
        # Inside cmd's quotes: one « " » more would end them.
        self.assertNotIn('"', command)
        self.assertIn("[IO.Path]::GetFullPath('%MM_APP%start_production.cmd')", command)
        self.assertIn("[char]34", command)
        refused = self.script.reachable("task_elsewhere")
        said = " ".join(refused)
        self.assertIn("REFUS : la tache %MM_TASK% ne lance pas %MM_APP%start_production.cmd", said)
        self.assertIn("DEPLOY.md section 7", said)
        for danger in ("schtasks", "Stop-Process", "merge", "manage.py", "etat"):
            self.assertFalse([line for line in self.script.executed(refused) if danger.lower() in line.lower()], danger)

    def test_a_job_started_under_the_stop_restarts_the_old_server(self):
        """A job started in the seconds between running_jobs and
        Stop-Process was killed without a word, and the deploy went on."""
        stopped = self.script.line_of("call :wait_for_port %MM_PORT% free 30")
        again = [index for index, line in self.script.commands() if line == '"%MM_PYTHON%" manage.py running_jobs']
        self.assertEqual(len(again), 2)
        self.assertLess(stopped, again[1])
        self.assertLess(again[1], self.script.line_of('"%MM_PYTHON%" manage.py backup_data'))
        self.assertEqual(self.script.after(again[1]), "if errorlevel 1 goto :job_cut_off")
        branch = self.script.reachable("job_cut_off")
        self.assertIn("call :restart_if_running", branch)
        said = " ".join(branch)
        self.assertIn("ATTENTION", said)
        self.assertIn("vient d'etre coupe", said)
        for danger in ("merge", "backup_data", "migrate_tenants", "reset"):
            self.assertFalse([line for line in self.script.executed(branch) if danger in line], danger)


class RefreshDevDataScriptTests(CmdHygieneMixin, SimpleTestCase):
    name = "refresh_dev_data.cmd"

    def test_it_runs_from_its_own_folder(self):
        commands = self.script.commands()
        self.assertEqual(commands[0][1], "setlocal EnableExtensions DisableDelayedExpansion")
        self.assertEqual(commands[1][1], 'cd /d "%~dp0"')

    def test_it_refuses_production_before_anything_moves(self):
        check = self.script.line_of(f'"%MM_PYTHON%" -c "{SNIPPET}" development "%MM_SOURCE%"')
        self.assertEqual(self.script.after(check), "if errorlevel 1 goto :refused")
        self.assertLess(check, self.script.line_of('move "%MM_DATA%" "%MM_PREVIOUS%"'))
        self.assertLess(check, self.script.line_of("robocopy"))
        for danger in ("move", "robocopy"):
            self.assertFalse([line for line in self.script.reachable("refused") if line.startswith(danger)], danger)

    def test_it_refuses_while_the_development_server_runs(self):
        """runserver (and the preview) holds the databases open: moved under
        it, the folder half goes, or it writes into the copy being made."""
        check = self.script.line_of("call :port_free 8000")
        self.assertEqual(self.script.after(check), "if errorlevel 1 goto :dev_server_running")
        self.assertLess(check, self.script.line_of('move "%MM_DATA%"'))
        self.assertNotIn(str(serve.DEFAULT_PORT), self.script.text)
        section = self.script.section("port_free")
        self.assertTrue(any("Get-NetTCPConnection -LocalPort %1 -State Listen" in line for line in section))

    def test_it_takes_the_newest_whole_backup(self):
        """By name, newest first: only a name made the way backup_data makes
        it, so an -INCOMPLET folder is never taken (the helper refuses one
        given by hand too) - and only a FINISHED one: a backup cut short by
        a closed window or a power cut keeps its plain name and has no
        manifest.json (written last), and taken, it made every refresh
        refuse until somebody found and deleted it."""
        self.assertIn('set "MM_BACKUPS=C:\\MarginMate\\backups"', self.script.text)
        self.assertIn('set "MM_SOURCE=%~1"', self.script.text)
        pick = self.script.lines[self.script.line_of('dir /b /ad /o-n "%MM_BACKUPS%"')]
        self.assertIn('findstr /r /x "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]"', pick)
        self.assertIn(
            'do if not defined MM_SOURCE if exist "%MM_BACKUPS%\\%%b\\manifest.json" '
            'if exist "%MM_BACKUPS%\\%%b\\data\\" set "MM_SOURCE=%MM_BACKUPS%\\%%b"',
            pick,
        )
        stamp = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}$")
        self.assertRegex("2026-10-01_101500", stamp)
        self.assertNotRegex("2026-10-01_101500-INCOMPLET", stamp)

    def test_it_never_deletes_and_never_copies_the_env(self):
        """robocopy names .env only among the files it leaves out (/XF)."""
        for _, line in self.script.commands():
            lowered = line.lower()
            if lowered.startswith("echo"):
                continue
            with self.subTest(line=line):
                self.assertIsNone(re.search(r"(^|[\s&(])(rd|rmdir|erase|xcopy)\s", lowered))
                for switch in ("/mir", "/purge", "/mov"):
                    self.assertNotIn(switch, lowered)
                if lowered.startswith("del "):
                    self.assertIn("%MM_ANSWERS%", line)
                if lowered.startswith(("copy", "robocopy", "move")):
                    self.assertNotIn(".env", re.sub(r" /xf [^/]*", " ", lowered))
        move = self.script.line_of('move "%MM_DATA%" "%MM_PREVIOUS%"')
        self.assertEqual(self.script.after(move), "if errorlevel 1 goto :rename_failed")

    def test_robocopy_s_exit_codes(self):
        """robocopy says 1 when it copied files: only 8 and above fail."""
        copy = self.script.line_of('robocopy "%MM_BACKUP%\\data" "%MM_DATA%" /E')
        self.assertEqual(self.script.after(copy), "if errorlevel 8 goto :copy_failed")

    def test_the_passwords_and_the_debug_pages_are_not_copied(self):
        """The « Identifiants » store is in no development copy
        (accounts/vault.py) - and a backup made before backup_data left it
        out still holds it - nor are the scrapers' failure dumps, which can
        print an account's identifiers: the same names backup_data leaves
        out, and the window says so."""
        copy = self.script.lines[self.script.line_of('robocopy "%MM_BACKUP%\\data" "%MM_DATA%" /E')]
        files = copy.split(" /XF ", 1)[1].split(" /", 1)[0].split()
        self.assertEqual(files, [*vault.FILE_NAMES, f"{vault.TEMPORARY_PREFIX}*.tmp", *deployment.ENV_COPY_PATTERNS])
        self.assertEqual(files[:2], list(data_backup.LEFT_OUT_FILES))
        folders = copy.split(" /XD ", 1)[1].split(" /", 1)[0].split()
        self.assertEqual(folders, [website.DEBUG_DIR])
        self.assertEqual(data_backup.DEBUG_FOLDER, website.DEBUG_DIR)
        said = [line for _, line in self.script.commands() if line.startswith("echo") and "Identifiants" in line]
        self.assertEqual(len(said), 1)
        self.assertIn("mots de passe", said[0])
        self.assertIn("pages de debug", said[0])
        self.assertIn("pas ete recopies", said[0])

    def test_a_copy_of_a_env_is_never_copied_and_the_window_says_so(self):
        """A copy of a .env (« .env.bak_<date> ») left in the data folder by
        hand is in every backup, and comes with it into data-dev: robocopy leaves
        out what backup_data does (`deployment.ENV_COPY_PATTERNS`), the
        patterns « .env » and « .env.* », and says so - before the copy and
        in the closing list. Step 3 has refused already while data-dev or
        the backup's data holds one (`DeploymentHelperTests`)."""
        self.assertEqual(deployment.ENV_COPY_PATTERNS, (".env", ".env.*"))
        self.assertIs(data_backup.is_env_copy, deployment.is_env_copy)
        copy = self.script.line_of('robocopy "%MM_BACKUP%\\data" "%MM_DATA%" /E')
        files = self.script.lines[copy].split(" /XF ", 1)[1].split(" /", 1)[0].split()
        self.assertEqual(files[-2:], [".env", ".env.*"])
        said = [(index, line) for index, line in self.script.commands() if line.startswith("echo") and ".env.*" in line]
        self.assertEqual(len(said), 2)
        (before, announced), (after, done) = said
        self.assertLess(before, copy)
        self.assertIn("ne sont pas recopiees", announced)
        self.assertLess(copy, after)
        self.assertIn(done, self.script.section("copy_data"))
        self.assertIn("n'a ete recopiee depuis les donnees de la sauvegarde", done)

    def test_the_sessions_the_copy_brought_are_forgotten(self):
        """A backup made before backup_data emptied them holds production's
        live sessions - a session key is a login on the public site, and
        data-dev is read by coding sessions. Once copied, the helper deletes
        them from data-dev's accounts database, behind the same refusals as
        `development`; a failure says so, ends in error, and moves nothing."""
        self.assertIn('set "MM_SESSIONS="', self.script.text)
        copy = self.script.line_of('robocopy "%MM_BACKUP%\\data" "%MM_DATA%" /E')
        forget = self.script.line_of(
            f'"%MM_PYTHON%" -c "{SNIPPET}" {deployment.PURGE_SESSIONS} "%MM_BACKUP%" > "%MM_ANSWERS%"'
        )
        self.assertLess(copy, forget)
        following = [line for _, line in self.script.commands(forget + 1)][:3]
        self.assertEqual(
            following,
            [
                "if errorlevel 1 goto :sessions_left",
                'for /f "usebackq tokens=1,* delims==" %%a in ("%MM_ANSWERS%") do set "MM_%%a=%%b"',
                'del "%MM_ANSWERS%" >nul 2>&1',
            ],
        )
        self.assertLess(forget, self.script.line_of('set "MM_CODE=0"'))
        self.assertTrue(
            any("%MM_SESSIONS%" in line for line in self.script.section("copy_data") if line.startswith("echo"))
        )
        branch = self.script.reachable("sessions_left")
        self.assertTrue(any(line.startswith("echo ATTENTION") for line in branch))
        self.assertNotIn('set "MM_CODE=0"', branch)
        for danger in ("move", "robocopy", "rd ", "rmdir"):
            self.assertFalse([line for line in branch if line.startswith(danger)], danger)

    def test_the_folder_set_aside_is_said_to_hold_the_old_copy_s_sessions(self):
        """The data-dev folder moved aside is neither purged nor deleted: it
        still holds the old copy's login sessions and the scrapers' debug
        pages. The closing message says so, and to delete it."""
        said = [line for line in self.script.section("copy_data") if line.startswith("if defined MM_PREVIOUS echo")]
        words = " ".join(said)
        for part in ("%MM_PREVIOUS%", "sessions de connexion", "pages de debug", "ancienne copie", "supprimez"):
            with self.subTest(part=part):
                self.assertIn(part, words)
        self.assertLess(
            self.script.line_of("if defined MM_PREVIOUS echo - les anciennes donnees"),
            self.script.line_of('set "MM_CODE=0"'),
        )

    def test_it_asks_first_and_reminds_what_the_copy_holds(self):
        ask = self.script.line_of("choice /C ON /N /M")
        self.assertEqual(self.script.after(ask), 'if not "%ERRORLEVEL%"=="1" goto :cancelled')
        self.assertLess(ask, self.script.line_of('move "%MM_DATA%"'))
        text = self.script.text
        self.assertIn("VRAIES donnees", text)
        self.assertIn("jamais dans git", text)
        self.assertIn("migrate_tenants", text)


@skipUnless(os.name == "nt", "cmd.exe and Windows PowerShell")
class OneLineOfTheScriptsRunAloneTests(SimpleTestCase):
    """A single line of a script, taken from it, run on its own against
    temporary folders or a stand-in: never the script itself (it stops the
    server, moves data folders). What cmd.exe and PowerShell make of a line
    is only proved by running it."""

    def setUp(self):
        super().setUp()
        self.folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-one-line-cmd-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    # -- refresh_dev_data.cmd's choice of a backup ------------------------------------------------------------------

    def backup(self, name: str, *, manifest=True, data="folder"):
        folder = self.folder / "backups" / name
        folder.mkdir(parents=True)
        if manifest:
            (folder / "manifest.json").write_text("{}", encoding="utf-8")
        if data == "folder":
            (folder / "data").mkdir()
        elif data == "file":
            (folder / "data").write_text("pas un dossier", encoding="ascii")

    def picked(self) -> str:
        line = Script("refresh_dev_data.cmd").lines[
            Script("refresh_dev_data.cmd").line_of('dir /b /ad /o-n "%MM_BACKUPS%"')
        ]
        batch = self.folder / "pick.cmd"
        batch.write_bytes(
            "\r\n".join(
                [
                    "@echo off",
                    "setlocal EnableExtensions DisableDelayedExpansion",
                    f'set "MM_BACKUPS={self.folder / "backups"}"',
                    'set "MM_SOURCE="',
                    line,
                    "echo SOURCE=%MM_SOURCE%",
                    "",
                ]
            ).encode("ascii")
        )
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(batch)], cwd=self.folder, capture_output=True, timeout=60, check=False
        )
        said = result.stdout.decode("ascii", "replace").strip().splitlines()
        return said[-1].split("=", 1)[1] if said else ""

    def test_the_newest_finished_backup_is_picked(self):
        self.backup("2026-10-01_101500")
        self.backup("2026-10-02_091500")
        # Newer, all unfinished: no manifest, an -INCOMPLET name, a « data »
        # that is a file, no data at all.
        self.backup("2026-10-03_101500", manifest=False)
        self.backup("2026-10-03_111500-INCOMPLET")
        self.backup("2026-10-02_235959", data="file")
        self.backup("2026-10-02_225959", data=None)
        self.assertEqual(self.picked(), str(self.folder / "backups" / "2026-10-02_091500"))

    def test_no_finished_backup_picks_none(self):
        self.backup("2026-10-03_101500", manifest=False)
        self.assertEqual(self.picked(), "")

    # -- refresh_dev_data.cmd's copy ---------------------------------------------------------------------------------

    def test_the_copy_leaves_the_passwords_and_the_debug_pages_out(self):
        """The robocopy line alone, from an invented backup made before
        backup_data left them out, into an empty folder: robocopy's /XF and
        /XD match by name at any depth, whatever the case - the copies of a
        .env exactly as `deployment.is_env_copy` tells them."""
        written = {
            "accounts.sqlite3": b"base inventee",
            ".env.bak_20990101": b"copie inventee d'un .env",
            "tenants/abc123/.env": b"copie inventee d'un .env",
            "tenants/abc123/private/.ENV.ANCIEN": b"copie inventee d'un .env",
            "tenants/abc123/.envoi.txt": b"pas un .env, invente",
            "tenants/abc123/media/notes.env": b"pas un .env non plus, invente",
            "tenants/abc123/private/cles/cle.pem": b"cle inventee",
            "tenants/abc123/private/credentials.bin": b"jeton invente",
            "tenants/abc123/private/credentials.key": b"cle scellee inventee",
            "tenants/abc123/private/.credentials-x1y2.tmp": b"ecriture coupee",
            "tenants/abc123/downloads/type-1/facture.pdf": b"%PDF-1.4 inventee",
            "tenants/abc123/downloads/type-1/_debug/x.html": b"<p>identifiant invente</p>",
            "tenants/abc123/downloads/test-12/_debug/y.png": b"capture inventee",
        }
        for relative, content in written.items():
            path = self.folder / "backup" / "data" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        script = Script("refresh_dev_data.cmd")
        line = script.lines[script.line_of('robocopy "%MM_BACKUP%\\data" "%MM_DATA%" /E')]
        target = self.folder / "data-dev"
        batch = self.folder / "copy.cmd"
        batch.write_bytes(
            "\r\n".join(
                [
                    "@echo off",
                    "setlocal EnableExtensions DisableDelayedExpansion",
                    f'set "MM_BACKUP={self.folder / "backup"}"',
                    f'set "MM_DATA={target}"',
                    line,
                    "echo CODE=%ERRORLEVEL%",
                    "",
                ]
            ).encode("ascii")
        )
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(batch)], cwd=self.folder, capture_output=True, timeout=120, check=False
        )
        said = result.stdout.decode("ascii", "replace").strip().splitlines()
        self.assertTrue(said and said[-1].startswith("CODE="), said[-5:])
        self.assertLess(int(said[-1].split("=", 1)[1]), 8)
        copied = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
        self.assertEqual(
            copied,
            [
                "accounts.sqlite3",
                "tenants/abc123/.envoi.txt",
                "tenants/abc123/downloads/type-1/facture.pdf",
                "tenants/abc123/media/notes.env",
                "tenants/abc123/private/cles/cle.pem",
            ],
        )
        self.assertEqual([path for path in target.rglob("*") if path.name == "_debug"], [])
        # robocopy and Python agree on what a copy of a .env is.
        for relative in written:
            name = relative.rsplit("/", 1)[-1]
            if "credentials" not in name and "_debug" not in relative:
                with self.subTest(file=relative):
                    self.assertIs(deployment.is_env_copy(name), relative not in copied)

    # -- deploy.cmd's check of the task's action ---------------------------------------------------------------------

    APP = "C:\\MarginMate\\app\\"

    def task_check(self, *executes) -> int:
        """deploy.cmd's PowerShell, with Get-ScheduledTask replaced by a
        function answering a task whose actions run `executes` (none: no
        task) - a function comes before a cmdlet in PowerShell's lookup, and
        the scheduler is never asked. Its exit code."""
        if executes:
            actions = ", ".join(
                "[pscustomobject]@{ Execute = '" + execute.replace("'", "''") + "' }" for execute in executes
            )
            answer = f"[pscustomobject]@{{ TaskName = $TaskName; Actions = @({actions}) }}"
        else:
            answer = "$null"
        stand_in = (
            "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName) "
            f"if ($TaskName -ne 'MarginMate') {{ throw 'another task' }}; {answer} }}; "
        )
        command = (
            task_check_command(Script("deploy.cmd")).replace("%MM_TASK%", "MarginMate").replace("%MM_APP%", self.APP)
        )
        encoded = base64.b64encode((stand_in + command).encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=self.folder,
            capture_output=True,
            timeout=120,
            check=False,
            env={**os.environ, "MM_TEST_ROOT": "C:\\MarginMate"},
        )
        return result.returncode

    # -- deploy.cmd's branches that only speak ------------------------------------------------------------------------

    #: Nothing a harness may run: every such line of the sections taken must be an echo.
    DANGERS = (
        "schtasks",
        "Stop-Process",
        "git ",
        "manage.py",
        "robocopy",
        "move ",
        "rmdir",
        "rd ",
        "del ",
        "mkdir",
        "start ",
    )

    def spoken(self, labels, **variables) -> str:
        """deploy.cmd's `labels` sections, in the file's order, run in a
        batch of their own after `variables` are set; :finish is a bare exit
        (no pause, no removal). What they print."""
        script = Script("deploy.cmd")
        body = []
        for label in labels:
            body.append(f":{label}")
            body += script.section(label)
        for line in body:
            if not line.lower().startswith("echo") and not line.startswith(":"):
                for danger in self.DANGERS:
                    self.assertNotIn(danger, line, "a harness never runs this")
        batch = self.folder / "branch.cmd"
        batch.write_bytes(
            "\r\n".join(
                [
                    "@echo off",
                    "setlocal EnableExtensions DisableDelayedExpansion",
                    *(f'set "{name}={value}"' for name, value in variables.items()),
                    f"goto :{labels[0]}",
                    *body,
                    ":finish",
                    "exit /b 0",
                    "",
                ]
            ).encode("ascii")
        )
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(batch)], cwd=self.folder, capture_output=True, timeout=60, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("ascii", "replace"))
        return result.stdout.decode("ascii", "replace")

    LEFT_HALF_WAY = (
        "already_running",
        "already_running_without_state",
        "mark_failed",
        "rollback_instructions",
        "rollback_without_backup",
        "rollback_restart",
    )

    def test_a_mark_that_could_not_be_made_is_not_another_run(self):
        """mkdir also fails without the right to write in .git: that is not
        « une autre mise en ligne »."""
        mark = self.folder / "app" / ".git" / "marginmate-deploy"
        said = self.spoken(
            self.LEFT_HALF_WAY, MM_APP=f"{self.folder / 'app'}\\", MM_LOCK=str(mark), MM_STATE=str(mark / "etat.txt")
        )
        self.assertIn("REFUS : impossible de creer le dossier", said)
        self.assertNotIn("autre mise en ligne", said)

    def test_a_mark_left_half_way_prints_the_way_back_from_what_it_noted(self):
        """etat.txt as deploy.cmd writes it, stopped at step 7: the next run
        reads it back and says the way back from what it noted."""
        mark = self.folder / "app" / ".git" / "marginmate-deploy"
        mark.mkdir(parents=True)
        commit = "0123456789abcdef0123456789abcdef01234567"
        (mark / "etat.txt").write_bytes(
            (
                f"ancien={commit}\r\ndonnees=C:\\MarginMate\\data\r\netape=l'arret du serveur\r\n"
                "etape=la sauvegarde (manage.py backup_data)\r\n"
                "sauvegarde=C:\\MarginMate\\backups\\2026-10-01_101500\r\n"
                "etape=l'installation des dependances (uv sync --locked --no-dev)\r\n"
            ).encode("ascii")
        )
        said = self.spoken(
            self.LEFT_HALF_WAY,
            MM_APP=f"{self.folder / 'app'}\\",
            MM_LOCK=str(mark),
            MM_STATE=str(mark / "etat.txt"),
            MM_PREVIOUS="",
            MM_BACKUP="",
        )
        self.assertIn("REFUS", said)
        self.assertIn(f"git reset --hard {commit}\r\n  {SYNC}\r\n", said)
        self.assertIsNone(PIP.search(said))
        self.assertIn("pendant\r\nl'installation des dependances (uv sync --locked --no-dev), apres", said)
        self.assertIn('move "C:\\MarginMate\\data" "C:\\MarginMate\\data.echec"', said)
        self.assertIn('robocopy "C:\\MarginMate\\backups\\2026-10-01_101500\\data" "C:\\MarginMate\\data" /E', said)
        self.assertIn(f'rmdir /s /q "{mark}"', said)
        self.assertIn("relancez deploy.cmd", said)
        self.assertTrue((mark / "etat.txt").is_file(), "the mark stays")

    def test_a_mark_left_before_the_backup_offers_no_restore(self):
        mark = self.folder / "app" / ".git" / "marginmate-deploy"
        mark.mkdir(parents=True)
        (mark / "etat.txt").write_bytes(
            b"ancien=abc123\r\ndonnees=C:\\MarginMate\\data\r\netape=l'arret du serveur\r\n"
        )
        said = self.spoken(
            self.LEFT_HALF_WAY, MM_APP=f"{self.folder / 'app'}\\", MM_LOCK=str(mark), MM_STATE=str(mark / "etat.txt")
        )
        self.assertIn("git reset --hard abc123", said)
        self.assertIn("Aucune sauvegarde n'a ete notee", said)
        self.assertNotIn("robocopy", said)

    def test_a_mark_with_nothing_noted(self):
        mark = self.folder / "app" / ".git" / "marginmate-deploy"
        mark.mkdir(parents=True)
        said = self.spoken(
            self.LEFT_HALF_WAY, MM_APP=f"{self.folder / 'app'}\\", MM_LOCK=str(mark), MM_STATE=str(mark / "etat.txt")
        )
        self.assertIn("Elle n'a rien note", said)
        self.assertIn(f'rmdir /s /q "{mark}"', said)
        self.assertNotIn("git reset", said)

    def test_the_mark_is_taken_by_one_run_only(self):
        """deploy.cmd's own line, run twice on a temporary .git: the first
        takes the mark, the second goes to :already_running."""
        line = Script("deploy.cmd").lines[Script("deploy.cmd").line_of(DeployScriptTests.LOCK)]
        mark = self.folder / "app" / ".git" / "marginmate-deploy"
        mark.parent.mkdir(parents=True)
        batch = self.folder / "mark.cmd"
        batch.write_bytes(
            "\r\n".join(
                [
                    "@echo off",
                    "setlocal EnableExtensions DisableDelayedExpansion",
                    f'set "MM_LOCK={mark}"',
                    line,
                    "echo TAKEN",
                    "exit /b 0",
                    ":already_running",
                    "echo REFUSED",
                    "exit /b 0",
                    "",
                ]
            ).encode("ascii")
        )
        answers = [
            subprocess.run(
                ["cmd.exe", "/d", "/c", str(batch)], cwd=self.folder, capture_output=True, timeout=60, check=False
            )
            .stdout.decode("ascii", "replace")
            .strip()
            for _ in range(2)
        ]
        self.assertEqual(answers, ["TAKEN", "REFUSED"])
        self.assertTrue(mark.is_dir())

    def test_uv_silent_says_how_to_make_it_answer(self):
        mark = self.folder / "app" / ".git" / "marginmate-deploy"
        said = self.spoken(("no_uv",), MM_APP="C:\\MarginMate\\app\\", MM_LOCK=str(mark))
        self.assertIn("REFUS : uv ne repond pas.", said)
        self.assertIn('  cd /d "C:\\MarginMate\\app\\"\r\n  mise install\r\n  uv --version\r\n', said)
        self.assertIn("(DEPLOY.md,\r\nsection 10.6)", said)
        self.assertIn("Rien n'a ete fait.", said)

    def test_uv_silent_with_a_mark_left_prints_the_way_back(self):
        mark = self.folder / "app" / ".git" / "marginmate-deploy"
        mark.mkdir(parents=True)
        (mark / "etat.txt").write_bytes(
            b"ancien=abc123\r\ndonnees=C:\\MarginMate\\data\r\netape=la relance du serveur\r\n"
        )
        said = self.spoken(
            ("no_uv", *self.LEFT_HALF_WAY),
            MM_APP=f"{self.folder / 'app'}\\",
            MM_LOCK=str(mark),
            MM_STATE=str(mark / "etat.txt"),
        )
        self.assertIn("REFUS : une autre mise en ligne a laisse sa marque", said)
        self.assertIn("git reset --hard abc123", said)
        self.assertNotIn("uv ne repond pas", said)

    def test_the_other_branches_that_only_speak(self):
        said = self.spoken(("offline",), MM_APP="C:\\MarginMate\\app\\", MM_PORT="8765", MM_PREVIOUS_SHORT="abc1234")
        self.assertIn("ATTENTION : rien n'ecoute sur 127.0.0.1:8765, le site est hors ligne.", said)
        self.assertIn(f"  git reset --hard VERSION-D-AVANT\r\n  {SYNC}\r\n", said)
        self.assertIsNone(PIP.search(said))
        said = self.spoken(("git_unreadable",), MM_APP="C:\\MarginMate\\app\\")
        self.assertIn('git config --global --add safe.directory "C:/MarginMate/app"', said)
        said = self.spoken(("task_elsewhere",), MM_APP="C:\\MarginMate\\app\\", MM_TASK="MarginMate")
        self.assertIn("REFUS : la tache MarginMate ne lance pas C:\\MarginMate\\app\\start_production.cmd", said)

    @skipUnless(POWERSHELL.is_file(), "Windows PowerShell")
    def test_the_task_s_action_must_be_this_folder_s_start_production(self):
        quote = '"'
        cases = {
            "no task: nothing to check": ((), 0),
            "the path between double quotes": ((f"{quote}{self.APP}start_production.cmd{quote}",), 0),
            "another case": (("c:\\marginmate\\APP\\Start_Production.cmd",), 0),
            "an environment variable": (("%MM_TEST_ROOT%\\app\\start_production.cmd",), 0),
            "a detour through ..": (("C:\\MarginMate\\app\\logs\\..\\start_production.cmd",), 0),
            "the right one as the second action": (("C:\\Windows\\notepad.exe", f"{self.APP}start_production.cmd"), 0),
            "the development folder": (
                ("C:\\Users\\vous\\Desktop\\Bar application gestion\\AdminMate\\start_production.cmd",),
                1,
            ),
            "a neighbouring folder": (("C:\\MarginMate\\app-ancien\\start_production.cmd",), 1),
            "an empty action": (("",), 1),
        }
        for case, (executes, expected) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(self.task_check(*executes), expected)


class DeploymentHelperTests(SimpleTestCase):
    """accounts/deployment.py, as the scripts call it. Every folder is a
    temporary one - production's data folder included (`production`), never
    C:\\MarginMate: the helper now asks the disk what a path really is."""

    def setUp(self):
        super().setUp()
        # Its real path: the data folder must be named by it.
        self.folder = Path(os.path.realpath(tempfile.mkdtemp(prefix="marginmate-tests-deployment-")))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.data = self.folder / "data-dev"
        self.production = self.folder / "production" / "data"

    @contextlib.contextmanager
    def settings_as(self, accounts=None, **overrides):
        """The settings a folder's .env would give: TENANTS_ROOT under
        self.data unless said, and the accounts database beside it (or
        `accounts`) - only the helper reads it, never opens it."""
        overrides.setdefault("TENANTS_ROOT", self.data / "tenants")
        accounts = accounts or Path(overrides["TENANTS_ROOT"]).parent / "accounts.sqlite3"
        databases = {**settings.DATABASES, "accounts": {**settings.DATABASES["accounts"], "NAME": str(accounts)}}
        with warnings.catch_warnings():
            # Overriding DATABASES warns: only the helper reads it here.
            warnings.simplefilter("ignore")
            with override_settings(DATABASES=databases, **overrides):
                yield

    def ask(self, *argv, accounts=None, **overrides) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with self.settings_as(accounts, **overrides):
            code = deployment.main(list(argv), stdout=out, stderr=err)
        answers = dict(line.split("=", 1) for line in out.getvalue().splitlines())
        return code, answers, err.getvalue()

    def backup(self, name="2026-10-01_101500", source=None, manifest=True) -> Path:
        """A finished backup, taken from `source` (production's data folder
        by default)."""
        folder = self.folder / "backups" / name
        (folder / "data" / "tenants").mkdir(parents=True)
        if manifest:
            source = str(self.production) if source is None else source
            (folder / "manifest.json").write_text(json.dumps({"source": source}), encoding="utf-8")
        return folder

    # -- production -----------------------------------------------------------------------------------------------

    def test_production_needs_debug_off_and_https(self):
        code, answers, said = self.ask("production", DEBUG=True, HTTPS=True)
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn("DJANGO_DEBUG=True : c'est le dossier de développement", said)
        code, answers, said = self.ask("production", DEBUG=False, HTTPS=False)
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn("ne dit pas MARGINMATE_HTTPS=1", said)

    def test_production_answers_its_data_folder(self):
        code, answers, said = self.ask("production", DEBUG=False, HTTPS=True)
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(answers, {"DATA": str(self.data)})

    def test_production_with_its_data_in_the_code_is_refused(self):
        code, _, said = self.ask("production", DEBUG=False, HTTPS=True, TENANTS_ROOT=BASE / "tenants")
        self.assertEqual(code, deployment.REFUSED)
        self.assertIn("est le dossier du code ou le contient", said)

    def test_production_with_its_accounts_database_elsewhere_is_refused(self):
        """backup_data would refuse it too - after deploy.cmd had stopped
        the server. Refused here, before anything is stopped."""
        elsewhere = self.folder / "ailleurs" / "accounts.sqlite3"
        code, answers, said = self.ask("production", DEBUG=False, HTTPS=True, accounts=elsewhere)
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn(f"la base des comptes ({elsewhere}) n'est pas dans le dossier des données", said)
        self.assertIn(f"MARGINMATE_ACCOUNTS_DB doit être {self.data / 'accounts.sqlite3'}", said)

    # -- development ----------------------------------------------------------------------------------------------

    def test_development_is_refused_in_production(self):
        code, answers, said = self.ask("development", str(self.backup()), DEBUG=False, HTTPS=True)
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn("MARGINMATE_HTTPS=1 : c'est la copie de production", said)

    def test_the_code_s_folder_is_never_the_data_folder(self):
        """TENANTS_ROOT at its default (beside manage.py) makes the code's
        folder the data folder, and the folder holding it when it names a
        sibling: moved aside, either would take the code with it."""
        backup = self.backup()
        for root in (BASE / "tenants", BASE.parent / "tenants"):
            with self.subTest(root=str(root)):
                code, answers, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False, TENANTS_ROOT=root)
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn("dossier du code", said)

    def test_only_a_finished_backup_is_taken(self):
        cases = {
            "introuvable": self.folder / "backups" / "absente",
            "a échoué": self.backup(name="2026-10-01_101500-INCOMPLET"),
            "pas une sauvegarde terminée": self.backup(name="2026-10-02_101500", manifest=False),
        }
        for said_part, backup in cases.items():
            with self.subTest(said=said_part):
                code, answers, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False)
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn(said_part, said)
        broken = self.backup(name="2026-10-03_101500")
        (broken / "manifest.json").write_text("{pas du json", encoding="utf-8")
        code, _, said = self.ask("development", str(broken), DEBUG=True, HTTPS=False)
        self.assertEqual(code, deployment.REFUSED)
        self.assertIn("ne se lit pas", said)

    def test_the_production_s_own_data_folder_is_refused(self):
        """A development .env still pointing at the site's data (the state
        before the split): the refresh would move the site's data aside."""
        self.data.mkdir()
        backup = self.backup(source=str(self.data))
        code, answers, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn("ce sont les données du site", said)
        self.assertTrue(self.data.is_dir())

    def test_the_production_s_accounts_database_is_refused(self):
        """The development .env moved to data-dev\\tenants but kept
        MARGINMATE_ACCOUNTS_DB=C:\\MarginMate\\data\\accounts.sqlite3: the
        refresh passed, and DEPLOY 10.5's migrate_tenants then migrated the
        LIVE logins database under the running serve - every runserver
        writing its sessions into it besides."""
        production = self.folder / "production" / "data"
        backup = self.backup(source=str(production))
        code, answers, said = self.ask(
            "development", str(backup), DEBUG=True, HTTPS=False, accounts=production / "accounts.sqlite3"
        )
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn(f"la base des comptes ({production / 'accounts.sqlite3'}) n'est pas dans le dossier des", said)
        self.assertIn(f"MARGINMATE_ACCOUNTS_DB doit être {self.data / 'accounts.sqlite3'}", said)
        # Anywhere else outside data-dev too.
        code, _, said = self.ask(
            "development", str(backup), DEBUG=True, HTTPS=False, accounts=self.folder / "comptes.sqlite3"
        )
        self.assertEqual(code, deployment.REFUSED)
        # Inside it, under another name, it is data-dev's own.
        code, _, said = self.ask(
            "development", str(backup), DEBUG=True, HTTPS=False, accounts=self.data / "comptes" / "base.sqlite3"
        )
        self.assertEqual((code, said), (0, ""))

    def test_a_backup_inside_the_data_folder_is_refused(self):
        backup = self.data / "sauvegarde"
        (backup / "data").mkdir(parents=True)
        (backup / "manifest.json").write_text(json.dumps({"source": str(self.production)}), encoding="utf-8")
        code, _, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual(code, deployment.REFUSED)
        self.assertIn("l'un dans l'autre", said)

    def test_development_answers_where_everything_goes(self):
        backup = self.backup()
        code, answers, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, said), (0, ""))
        # No data folder yet: nothing to move aside.
        self.assertEqual(answers, {"DATA": str(self.data), "BACKUP": str(backup)})

        self.data.mkdir()
        moment = datetime(2026, 10, 2, 9, 30, 5)
        with self.settings_as(DEBUG=True, HTTPS=False):
            answers = deployment.development(settings, str(backup), now=moment)
        self.assertEqual(answers["PREVIOUS"], str(self.folder / "data-dev.ancien-2026-10-02_093005"))
        # Never the name of a folder already there.
        (self.folder / "data-dev.ancien-2026-10-02_093005").mkdir()
        with self.settings_as(DEBUG=True, HTTPS=False):
            answers = deployment.development(settings, str(backup), now=moment)
        self.assertEqual(answers["PREVIOUS"], str(self.folder / "data-dev.ancien-2026-10-02_093005-2"))

    #: This folder's key in the tests below - invented.
    OWN_KEY = "cle-inventee-du-dossier-de-developpement-" + "k7Qz" * 10

    def test_a_backup_made_with_this_folder_s_secret_key_is_refused(self):
        """The development copy must have its own DJANGO_SECRET_KEY: with
        production's it opens production's sessions (an older backup holds
        them) and whatever is sealed with it - half of the « Identifiants »
        store's key, the signed cookies, the browsers' storage scopes. The
        backup's .env is read as the settings read one (python-dotenv), the
        keys compared in constant time and never printed."""
        written = (
            f"DJANGO_SECRET_KEY={self.OWN_KEY}\n",
            f'DJANGO_DEBUG=False\nDJANGO_SECRET_KEY="{self.OWN_KEY}"\nMARGINMATE_HTTPS=1\n',
            f"export DJANGO_SECRET_KEY='{self.OWN_KEY}'\n",
            f"DJANGO_SECRET_KEY = {self.OWN_KEY}  # la cle\n",
        )
        for number, text in enumerate(written):
            with self.subTest(env=text.splitlines()[0][:24]):
                backup = self.backup(name=f"2026-10-01_10150{number}")
                (backup / ".env").write_text(text, encoding="utf-8")
                for mode in ("development", deployment.PURGE_SESSIONS):
                    code, answers, said = self.ask(mode, str(backup), DEBUG=True, HTTPS=False, SECRET_KEY=self.OWN_KEY)
                    self.assertEqual((code, answers), (deployment.REFUSED, {}))
                    self.assertIn("la même clé secrète", said)
                    self.assertIn("DJANGO_SECRET_KEY", said)
                    self.assertNotIn(self.OWN_KEY, said)
                    self.assertNotIn("k7Qz", said)
        # Another key, no .env (--sans-env), no key or an empty one in it: taken.
        for number, text in enumerate(
            (f"DJANGO_SECRET_KEY={self.OWN_KEY}x\n", None, "DJANGO_DEBUG=False\n", "DJANGO_SECRET_KEY=\n")
        ):
            with self.subTest(env=str(text)[:24]):
                backup = self.backup(name=f"2026-10-02_10150{number}")
                if text is not None:
                    (backup / ".env").write_text(text, encoding="utf-8")
                code, _, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False, SECRET_KEY=self.OWN_KEY)
                self.assertEqual((code, said), (0, ""))

    def test_a_backup_s_env_that_does_not_read_is_refused(self):
        """Nothing then says whose key it holds."""
        backup = self.backup()
        (backup / ".env").write_bytes(b"DJANGO_SECRET_KEY=\xff\xfe pas de l'UTF-8\n")
        code, answers, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn("ne se lit pas", said)

    def accounts_copy(self, database: Path | None = None) -> Path:
        """An accounts database as robocopy leaves one in data-dev (WAL, as
        production writes it): a login table and Django's session table,
        two sessions - every value invented."""
        database = database or self.data / "accounts.sqlite3"
        database.parent.mkdir(parents=True, exist_ok=True)
        table = Session._meta.db_table
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE comptes (email TEXT)")
        connection.execute("INSERT INTO comptes VALUES ('alpha@example.invalid')")
        connection.execute(
            f'CREATE TABLE "{table}" (session_key varchar(40) NOT NULL PRIMARY KEY, '
            "session_data text NOT NULL, expire_date datetime NOT NULL)"
        )
        connection.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?)',
            [(f"cle-de-session-inventee-{n}", "donnees-inventees-" * 30, "2026-10-15 08:00:00") for n in (1, 2)],
        )
        connection.commit()
        connection.close()
        return database

    @staticmethod
    def sessions_in(database: Path) -> int:
        connection = sqlite3.connect(database)
        try:
            return connection.execute(f'SELECT COUNT(*) FROM "{Session._meta.db_table}"').fetchone()[0]
        finally:
            connection.close()

    def test_the_sessions_a_copy_brought_are_forgotten(self):
        self.assertEqual(deployment.SESSION_TABLE, Session._meta.db_table)
        self.assertEqual(deployment.SESSION_TABLE, data_backup.SESSION_TABLE)
        self.assertEqual(deployment.ACCOUNTS_ROLE, data_backup.ACCOUNTS_ROLE)
        backup = self.backup()
        database = self.accounts_copy()
        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(answers, {"SESSIONS": "2"})
        self.assertEqual(self.sessions_in(database), 0)
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("SELECT email FROM comptes").fetchall(), [("alpha@example.invalid",)])
        finally:
            connection.close()
        # Rewritten without them, and nothing left beside it.
        self.assertNotIn(b"cle-de-session-inventee", database.read_bytes())
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["accounts.sqlite3"])
        # Again: nothing left to forget.
        code, answers, _ = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers), (0, {"SESSIONS": "0"}))

    def test_the_push_devices_a_copy_brought_are_forgotten_with_its_sessions(self):
        """data-dev never pushes (its SECRET_KEY, so its VAPID key, is its
        own): the browsers' endpoints and keys a copy brought are exposure
        only, emptied with the sessions and rewritten out of the file. A
        database with the device table and no session table is emptied
        too; the answers stay what refresh_dev_data.cmd reads."""
        self.assertEqual(deployment.PUSH_DEVICE_TABLE, PushDevice._meta.db_table)
        backup = self.backup()
        database = self.accounts_copy()
        older = self.data / "accounts.sqlite3.bak_20990101_pre_x"
        for path, sessions in ((database, True), (older, False)):
            if not sessions:
                older.write_bytes(b"")
            connection = sqlite3.connect(path)
            connection.execute(f'CREATE TABLE "{deployment.PUSH_DEVICE_TABLE}" (endpoint TEXT, auth TEXT)')
            connection.execute(
                f'INSERT INTO "{deployment.PUSH_DEVICE_TABLE}" VALUES (?, ?)',
                ("https://fcm.googleapis.com/fcm/send/jeton-invente-de-copie", "secret-invente-16"),
            )
            connection.commit()
            connection.close()
        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(answers, {"SESSIONS": "2"})
        for path in (database, older):
            with self.subTest(database=path.name):
                connection = sqlite3.connect(path)
                try:
                    rows = connection.execute(f'SELECT COUNT(*) FROM "{deployment.PUSH_DEVICE_TABLE}"').fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(rows, 0)
                self.assertNotIn(b"jeton-invente-de-copie", path.read_bytes())
        self.assertEqual(self.sessions_in(database), 0)

    def test_the_copy_s_accounts_database_is_the_one_its_manifest_names_never_outside(self):
        """A development .env may name another file inside data-dev: the one
        the copy brought is emptied all the same. A path of the manifest
        leading out of data-dev is never opened, and no database is made."""
        backup = self.backup()
        outside = self.accounts_copy(self.folder / "dehors.sqlite3")
        databases = [
            {"path": "accounts.sqlite3", "role": data_backup.ACCOUNTS_ROLE},
            {"path": "../dehors.sqlite3", "role": data_backup.ACCOUNTS_ROLE},
            {"path": str(outside), "role": data_backup.ACCOUNTS_ROLE},
            {"path": "tenants/x/db.sqlite3", "role": data_backup.TENANT_ROLE},
        ]
        manifest = {"source": str(self.production), "databases": databases}
        (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        copied = self.accounts_copy()
        own = self.data / "comptes" / "base.sqlite3"
        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False, accounts=own)
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(answers, {"SESSIONS": "2"})
        self.assertEqual((self.sessions_in(copied), self.sessions_in(outside)), (0, 2))
        self.assertFalse(own.exists())
        self.assertFalse((self.data / "tenants").exists())

    def test_forgetting_sessions_is_refused_where_development_is(self):
        """Never production's logins: the refusals of `development`, before
        any database is opened."""
        backup = self.backup()
        database = self.accounts_copy()
        production = self.folder / "production" / "data"
        live = self.accounts_copy(production / "accounts.sqlite3")
        cases = {
            "c'est la copie de production": {"DEBUG": False, "HTTPS": True},
            "n'est pas dans le dossier des données": {
                "DEBUG": True,
                "HTTPS": False,
                "accounts": production / "accounts.sqlite3",
            },
        }
        for said_part, overrides in cases.items():
            with self.subTest(said=said_part):
                code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), **overrides)
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn(said_part, said)
        taken_from_here = self.backup(name="2026-10-02_101500", source=str(self.data))
        code, _, said = self.ask(deployment.PURGE_SESSIONS, str(taken_from_here), DEBUG=True, HTTPS=False)
        self.assertEqual(code, deployment.REFUSED)
        self.assertIn("ce sont les données du site", said)
        self.assertEqual((self.sessions_in(database), self.sessions_in(live)), (2, 2))

    def test_no_accounts_database_forgets_nothing_and_a_broken_one_is_said(self):
        backup = self.backup()
        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers, said), (0, {"SESSIONS": "0"}, ""))
        self.assertFalse((self.data / "accounts.sqlite3").exists())
        self.data.mkdir()
        (self.data / "accounts.sqlite3").write_bytes(b"pas une base SQLite, un texte invente " * 40)
        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers), (1, {}))
        self.assertIn("ÉCHEC : les sessions de connexion de", said)
        self.assertIn("n'ont pas pu être effacées", said)

    def test_every_database_of_the_copy_is_purged(self):
        """A backup made before backup_data copied every database through
        SQLite holds, beside the accounts database, a copy of it made by hand
        (accounts.sqlite3.bak_…) and the database kept from before an
        adoption, sessions included. Every file of data-dev that starts like
        an SQLite database and holds Django's session table is emptied and
        rewritten, whatever its name; nothing else is written."""
        backup = self.backup()
        tenant = self.data / "tenants" / "abc123"
        purged = [
            self.accounts_copy(),
            self.accounts_copy(self.data / "accounts.sqlite3.bak_20990101_pre_x"),
            self.accounts_copy(tenant / "db.sqlite3.bak_20260928_pre_adoption"),
        ]
        safety = tenant / "backups" / "2026-09-19_143012_avant-import.sqlite3"
        safety.parent.mkdir()
        connection = sqlite3.connect(safety)
        connection.execute("CREATE TABLE fournisseurs (nom TEXT)")
        connection.execute("INSERT INTO fournisseurs VALUES ('Grossiste Alpha')")
        connection.commit()
        connection.close()
        named = tenant / "media" / "notes.sqlite3"
        named.parent.mkdir(parents=True)
        named.write_bytes(b"cle-de-session-inventee-9, un texte invente\n")
        untouched = {path: path.read_bytes() for path in (safety, named)}

        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, said), (0, ""))
        self.assertEqual(answers, {"SESSIONS": "6"})
        for database in purged:
            with self.subTest(database=database.name):
                self.assertEqual(self.sessions_in(database), 0)
                self.assertNotIn(b"cle-de-session-inventee", database.read_bytes())
                connection = sqlite3.connect(database)
                try:
                    emails = connection.execute("SELECT email FROM comptes").fetchall()
                finally:
                    connection.close()
                self.assertEqual(emails, [("alpha@example.invalid",)])
        for path, content in untouched.items():
            with self.subTest(untouched=path.name):
                self.assertEqual(path.read_bytes(), content)
        sides = [path.name for path in self.data.rglob("*") if path.name.endswith(("-wal", "-shm", "-journal"))]
        self.assertEqual(sides, [])
        code, answers, _ = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers), (0, {"SESSIONS": "0"}))

    def test_one_database_that_will_not_purge_holds_back_none_of_the_others(self):
        """Said, the step failed (exit 1: the script says « ATTENTION ») -
        and every other database emptied all the same."""
        backup = self.backup()
        accounts = self.accounts_copy()
        broken = self.data / "tenants" / "abc123" / "backups" / "abimee.sqlite3"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(deployment.SQLITE_HEADER + b"pas la suite d'une base, un texte invente " * 100)
        later = self.accounts_copy(self.data / "tenants" / "abc123" / "db.sqlite3.bak_20260928_pre_adoption")
        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers), (1, {}))
        self.assertIn(f"ÉCHEC : les sessions de connexion de {broken} n'ont pas pu être effacées", said)
        self.assertEqual((self.sessions_in(accounts), self.sessions_in(later)), (0, 0))

    @skipUnless(os.name == "nt", "a directory junction")
    def test_a_junction_out_of_the_copy_is_never_followed(self):
        """os.walk goes down a directory junction (Python 3.11 takes it for
        a folder): a database it reaches outside data-dev is never opened."""
        import _winapi

        backup = self.backup()
        outside = self.accounts_copy(self.folder / "ailleurs" / "accounts.sqlite3")
        junction = self.data / "tenants" / "lien"
        junction.parent.mkdir(parents=True)
        _winapi.CreateJunction(str(outside.parent), str(junction))
        self.addCleanup(os.rmdir, junction)
        self.assertTrue((junction / "accounts.sqlite3").is_file())
        code, answers, said = self.ask(deployment.PURGE_SESSIONS, str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers, said), (0, {"SESSIONS": "0"}, ""))
        self.assertEqual(self.sessions_in(outside), 2)

    # -- A folder by another name ----------------------------------------------------------------------------------

    MODES = ("development", deployment.PURGE_SESSIONS)

    @skipUnless(os.name == "nt", "mklink /J")
    def test_a_junction_to_the_production_s_data_is_refused(self):
        """A development .env naming a junction that leads to production's
        data folder: as abspath strings, « …\\data-lien » and
        « C:\\MarginMate\\data » are two folders, and the refresh went on
        towards the site's data. Resolved, they are one. Nothing is opened:
        production's sessions stay."""
        live = self.accounts_copy(self.production / "accounts.sqlite3")
        backup = self.backup()
        junction = make_junction(self, self.folder / "data-lien", self.production)
        for mode in self.MODES:
            with self.subTest(mode=mode):
                code, answers, said = self.ask(
                    mode, str(backup), DEBUG=True, HTTPS=False, TENANTS_ROOT=junction / "tenants"
                )
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn(f"le dossier des données de ce dossier ({junction}) est celui dont la sauvegarde", said)
                self.assertIn("ce sont les données du site", said)
        self.assertEqual(self.sessions_in(live), 2)
        # In the production copy too: DATA= must name the real folder.
        code, answers, said = self.ask("production", DEBUG=False, HTTPS=True, TENANTS_ROOT=junction / "tenants")
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn(f"le dossier des données de ce dossier ({junction}) n'est pas nommé par son vrai chemin", said)
        self.assertIn(f"il mène à {self.production}", said)

    def test_the_source_by_another_name_is_still_this_folder(self):
        """The manifest names the folder the backup was taken from as the
        production's .env named it. Another name of data-dev there - a
        junction, the \\\\?\\ prefix, another case, its 8.3 short name - is
        data-dev all the same, in both directions."""
        self.data.mkdir()
        aliases = {
            "préfixe": "\\\\?\\" + str(self.data),
            "casse": str(self.data).upper(),
            "dossier au-dessus, préfixé": "\\\\?\\" + str(self.folder),
        }
        if os.name == "nt":
            aliases["jonction"] = str(make_junction(self, self.folder / "lien-vers-data-dev", self.data))
        short = short_name(self.data)
        if short:
            aliases["nom court"] = short
        for number, (alias, source) in enumerate(aliases.items()):
            with self.subTest(alias=alias):
                backup = self.backup(name=f"2026-10-01_1015{number:02d}", source=source)
                for mode in self.MODES:
                    code, answers, said = self.ask(mode, str(backup), DEBUG=True, HTTPS=False)
                    self.assertEqual((code, answers), (deployment.REFUSED, {}), mode)
                    self.assertIn("ce sont les données du site", said)

    def test_the_data_folder_must_be_named_by_its_real_path(self):
        """DATA= is what refresh_dev_data.cmd moves aside and copies into, and
        what every comparison is made from: named through a junction, a
        symbolic link, its 8.3 short name or the \\\\?\\ prefix, it is
        refused, its real path said."""
        self.data.mkdir()
        backup = self.backup()
        names = {"préfixe": Path("\\\\?\\" + str(self.data))}
        if os.name == "nt":
            names["jonction"] = make_junction(self, self.folder / "autre-nom", self.data)
        try:
            os.symlink(self.data, self.folder / "lien-symbolique", target_is_directory=True)
        except OSError:
            pass  # Windows makes symbolic links for an administrator or in developer mode only.
        else:
            self.addCleanup(os.unlink, self.folder / "lien-symbolique")
            names["lien symbolique"] = self.folder / "lien-symbolique"
        short = short_name(self.data)
        if short:
            names["nom court"] = Path(short)
        for name, data in names.items():
            with self.subTest(name=name):
                for mode in self.MODES:
                    code, answers, said = self.ask(
                        mode, str(backup), DEBUG=True, HTTPS=False, TENANTS_ROOT=data / "tenants"
                    )
                    self.assertEqual((code, answers), (deployment.REFUSED, {}), mode)
                    self.assertIn("n'est pas nommé par son vrai chemin", said)
                    self.assertIn(f"il mène à {self.data}", said)
                    self.assertIn("MARGINMATE_TENANTS_ROOT", said)

    @skipUnless(os.name == "nt", "mklink /J")
    def test_the_tenants_folder_by_another_name_is_refused(self):
        """data-dev itself real, its tenants\\ a junction to production's: the
        pages would serve the site's espaces."""
        (self.production / "tenants").mkdir(parents=True)
        self.data.mkdir()
        tenants = make_junction(self, self.data / "tenants", self.production / "tenants")
        code, answers, said = self.ask("development", str(self.backup()), DEBUG=True, HTTPS=False)
        self.assertEqual((code, answers), (deployment.REFUSED, {}))
        self.assertIn(f"le dossier des espaces de ce dossier ({tenants}) n'est pas nommé par son vrai chemin", said)
        self.assertIn(f"il mène à {self.production / 'tenants'}", said)

    @skipUnless(os.name == "nt", "mklink /J")
    def test_an_accounts_database_through_a_junction_out_of_the_data_folder_is_refused(self):
        """« …\\data-dev\\lien\\accounts.sqlite3 » reads as inside data-dev; the
        file is production's. The accounts database must be inside the data
        folder as written AND as resolved."""
        live = self.accounts_copy(self.production / "accounts.sqlite3")
        backup = self.backup()
        link = make_junction(self, self.data / "lien", self.production)
        for mode in self.MODES:
            with self.subTest(mode=mode):
                code, answers, said = self.ask(
                    mode, str(backup), DEBUG=True, HTTPS=False, accounts=link / "accounts.sqlite3"
                )
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn(
                    f"la base des comptes ({link / 'accounts.sqlite3'}), qui mène à {live}, n'est pas dans le dossier",
                    said,
                )
        self.assertEqual(self.sessions_in(live), 2)

    @skipUnless(os.name == "nt", "mklink /J")
    def test_a_backup_by_another_name_inside_the_data_folder_is_refused(self):
        inner = self.data / "sauvegarde"
        (inner / "data").mkdir(parents=True)
        (inner / "manifest.json").write_text(json.dumps({"source": str(self.production)}), encoding="utf-8")
        link = make_junction(self, self.folder / "lien-sauvegarde", inner)
        code, _, said = self.ask("development", str(link), DEBUG=True, HTTPS=False)
        self.assertEqual(code, deployment.REFUSED)
        self.assertIn("l'un dans l'autre", said)

    @skipUnless(os.name == "nt", "mklink /J")
    def test_the_code_s_folder_by_another_name_is_refused(self):
        """A stand-in for the code's folder (never the real one: a junction
        towards it would put the code under a temporary folder)."""
        code_folder = self.folder / "code"
        code_folder.mkdir()
        junction = make_junction(self, self.folder / "lien-code", code_folder)
        for number, root in enumerate((junction / "tenants", junction / "data-dev" / "tenants")):
            with self.subTest(root=str(root)):
                backup = self.backup(name=f"2026-10-01_10150{number}")
                code, answers, said = self.ask(
                    "development", str(backup), DEBUG=True, HTTPS=False, TENANTS_ROOT=root, BASE_DIR=code_folder
                )
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn("est le dossier du code ou le contient", said)

    @skipUnless(os.name == "nt", "a directory junction")
    def test_inside_by_every_name(self):
        outer = self.folder / "dehors"
        (outer / "tenants").mkdir(parents=True)
        junction = make_junction(self, self.folder / "lien", outer)
        cases = {
            (junction / "tenants", outer): True,
            (outer / "tenants", junction): True,
            (junction, outer): True,
            (Path("\\\\?\\" + str(outer)) / "x", outer): True,
            (Path(str(outer).upper()) / "x", outer): True,
            (outer.with_name("dehors-aussi"), outer): False,
            (outer, outer / "tenants"): False,
        }
        for (inner, around), expected in cases.items():
            with self.subTest(inner=str(inner), outer=str(around)):
                self.assertIs(deployment.inside(inner, around), expected)
        # Opened, or relied upon, as inside: as written AND as resolved.
        self.assertTrue(deployment.really_inside(outer / "tenants", outer))
        self.assertFalse(deployment.really_inside(junction / "tenants", outer))
        self.assertFalse(deployment.really_inside(outer / "tenants", junction))
        (self.folder / "ailleurs").mkdir()
        inner_link = make_junction(self, outer / "vers-ailleurs", self.folder / "ailleurs")
        self.assertTrue(deployment.inside(inner_link / "accounts.sqlite3", outer))
        self.assertFalse(deployment.really_inside(inner_link / "accounts.sqlite3", outer))

    # -- A copy of a .env --------------------------------------------------------------------------------------------

    def test_a_copy_of_a_env_in_data_dev_or_in_the_backup_is_refused(self):
        """A copy of a .env in the data folder comes into data-dev with the
        rest: the site's secret key and passwords, where coding sessions
        read. development and purge-sessions refuse while
        data-dev, or the data the backup brings, holds a copy of a .env -
        named, never opened - and say to delete it."""
        backup = self.backup()
        self.data.mkdir()
        copied = self.data / ".env.bak_20990101"
        copied.write_text("DJANGO_SECRET_KEY=cle-inventee-copiee-par-le-test\n", encoding="utf-8")
        for mode in self.MODES:
            with self.subTest(mode=mode, where="data-dev"):
                code, answers, said = self.ask(mode, str(backup), DEBUG=True, HTTPS=False)
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn(
                    f"le dossier des données de ce dossier ({self.data}) contient une copie d'un fichier .env : "
                    f"{copied}.",
                    said,
                )
                self.assertIn(
                    "la clé secrète et des mots de passe du site : supprimez-la (DEPLOY.md, section 12)", said
                )
                self.assertNotIn("cle-inventee-copiee", said)
        copied.unlink()

        in_backup = [backup / "data" / ".env", backup / "data" / "tenants" / "abc123" / ".ENV.ancien"]
        for path in in_backup:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("DJANGO_SECRET_KEY=cle-inventee-copiee-par-le-test\n", encoding="utf-8")
        for mode in self.MODES:
            with self.subTest(mode=mode, where="sauvegarde"):
                code, answers, said = self.ask(mode, str(backup), DEBUG=True, HTTPS=False)
                self.assertEqual((code, answers), (deployment.REFUSED, {}))
                self.assertIn(
                    f"les données de la sauvegarde {backup} contiennent 2 copies de fichiers .env : "
                    f"{in_backup[0]}, {in_backup[1]}.",
                    said,
                )
                self.assertIn(f"supprimez-les (et, dans {self.production}, les fichiers d'origine", said)
                self.assertNotIn("cle-inventee-copiee", said)
        for path in in_backup:
            path.unlink()

        # What is no copy of a .env is taken: names that only look like one,
        # and the backup's own .env, beside its data (read for its key only).
        for path in (self.data / ".envoi.txt", self.data / "notes.env", backup / "data" / "environnement.txt"):
            path.write_text("un texte invente\n", encoding="utf-8")
        (backup / ".env").write_text("DJANGO_SECRET_KEY=une-autre-cle-inventee-pour-ce-test\n", encoding="utf-8")
        code, _, said = self.ask("development", str(backup), DEBUG=True, HTTPS=False)
        self.assertEqual((code, said), (0, ""))

    @skipUnless(os.name == "nt", "mklink /J")
    def test_a_env_reached_through_a_junction_is_not_data_dev_s(self):
        """Nothing outside data-dev is data-dev's: the code's own .env,
        reached through a junction (here a stand-in), does not refuse."""
        elsewhere = self.folder / "ailleurs"
        elsewhere.mkdir()
        (elsewhere / ".env").write_text("DJANGO_DEBUG=True\n", encoding="utf-8")
        make_junction(self, self.data / "lien", elsewhere)
        code, _, said = self.ask("development", str(self.backup()), DEBUG=True, HTTPS=False)
        self.assertEqual((code, said), (0, ""))

    def test_what_a_copy_of_a_env_is(self):
        cases = {
            ".env": True,
            ".env.bak_20990101": True,
            ".ENV.OLD": True,
            ".env.": True,
            ".env.example": True,
            ".envoi.txt": False,
            "notes.env": False,
            "env.bak": False,
            "environnement.txt": False,
            "": False,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertIs(deployment.is_env_copy(name), expected)

    def test_settings_that_do_not_load_and_a_wrong_usage(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(deployment, "_settings", side_effect=ImproperlyConfigured("clé secrète trop courte")):
            self.assertEqual(deployment.main(["production"], stdout=out, stderr=err), 1)
        self.assertIn("ne se chargent pas : clé secrète trop courte", err.getvalue())
        self.assertEqual(out.getvalue(), "")
        for argv in ([], ["n-importe-quoi"], ["development"], [deployment.PURGE_SESSIONS]):
            with self.subTest(argv=argv):
                self.assertEqual(deployment.main(argv, stdout=io.StringIO(), stderr=io.StringIO()), 1)


#: The scripts' one-liner in a child process: the settings loaded from the
#: environment given, the project's .env never read (as test_serve.MANAGE).
CHILD = "import dotenv; dotenv.load_dotenv = lambda *args, **kwargs: False; "


class TheScriptsOneLinerTests(SimpleTestCase):
    """The exact -c the scripts run, taken from them, run as they run it."""

    def run_the_scripts_python(self, *args, **environment):
        folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-one-line-"))
        env = child_environment(
            DJANGO_SETTINGS_MODULE="config.settings",
            DJANGO_SECRET_KEY="essai-" + "k7Qz" * 16,
            MARGINMATE_TENANTS_ROOT=str(folder / "data" / "tenants"),
            MARGINMATE_ACCOUNTS_DB=str(folder / "data" / "accounts.sqlite3"),
            PYTHONIOENCODING="utf-8",
            **environment,
        )
        result = subprocess.run(
            [sys.executable, "-c", CHILD + SNIPPET, *args],
            cwd=BASE,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [], "asking made nothing")
        return result, folder

    def test_both_scripts_run_the_same_line(self):
        for name in ("deploy.cmd", "refresh_dev_data.cmd"):
            with self.subTest(script=name):
                snippets = re.findall(r'"%MM_PYTHON%" -c "([^"]*)"', Script(name).text)
                self.assertEqual(set(snippets), {SNIPPET})

    def test_production_in_a_child(self):
        result, folder = self.run_the_scripts_python("production", DJANGO_DEBUG="False", MARGINMATE_HTTPS="1")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.splitlines(), [f"DATA={folder / 'data'}"])
        result, _ = self.run_the_scripts_python("production", DJANGO_DEBUG="True", MARGINMATE_HTTPS="1")
        self.assertEqual(result.returncode, deployment.REFUSED)
        self.assertEqual(result.stdout, "")
        self.assertIn("REFUS : le .env de ce dossier dit DJANGO_DEBUG=True", result.stderr)

    def test_development_in_a_child_refuses_production(self):
        result, _ = self.run_the_scripts_python(
            "development", "C:\\MarginMate\\backups\\2026-10-01_101500", DJANGO_DEBUG="False", MARGINMATE_HTTPS="1"
        )
        self.assertEqual(result.returncode, deployment.REFUSED)
        self.assertIn("c'est la copie de production", result.stderr)


def section(markdown: str, title: str) -> str:
    return markdown.split(title, 1)[1].split("\n## ", 1)[0]


def code_blocks(markdown: str) -> list[str]:
    return markdown.split("```")[1::2]


class DeployDocumentTests(SimpleTestCase):
    """DEPLOY.md, section 10 and every place that says where production is."""

    def setUp(self):
        super().setUp()
        self.deploy = (BASE / "DEPLOY.md").read_text(encoding="utf-8")

    def test_every_path_given_for_the_env_is_written_with_forward_slashes(self):
        # 30/09, the switch to C:\MarginMate: the production .env was written
        # with « "C:\MarginMate\data\tenants" » and the server said « La base
        # des comptes est introuvable » - between double quotes python-dotenv
        # reads \t as a TAB and \a as a BEL. The owner's own .env writes C:/…,
        # and every example the document gives must be copyable as it is.
        lines = [
            line.strip()
            for line in self.deploy.splitlines()
            if re.match(r"\s*MARGINMATE_(TENANTS_ROOT|ACCOUNTS_DB|LOG_DIR)=\S", line)
        ]
        self.assertTrue(lines)
        for line in lines:
            with self.subTest(line=line):
                self.assertNotIn("\\", line)
        self.assertIn("**Écrivez les chemins avec des barres obliques `/`**", self.deploy)

    def test_the_trap_the_forward_slashes_avoid_is_real(self):
        from io import StringIO

        from dotenv import dotenv_values

        read = dotenv_values(stream=StringIO('A="C:\\MarginMate\\data\\tenants"\nB="C:/MarginMate/data/tenants"\n'))
        self.assertIn("\t", read["A"])
        self.assertEqual(read["B"], "C:/MarginMate/data/tenants")

    def test_section_10_is_the_two_copies_workflow(self):
        self.assertNotIn("## 10. Mettre à jour le code", self.deploy)
        tenth = section(self.deploy, "## 10. Développer et mettre en ligne une modification")
        words = " ".join(tenth.split())
        for said in (
            "C:\\MarginMate\\app",
            "C:\\MarginMate\\data",
            "C:\\MarginMate\\backups",
            "data-dev",
            "deploy.cmd",
            "refresh_dev_data.cmd",
            "manage.py running_jobs",
            "manage.py backup_data",
            "git merge --ff-only origin/main",
            "manage.py migrate_tenants",
            "manage.py serve --verifier",
            "git reset --hard",
            "robocopy",
            "Déployer ces changements ? (O/N)",
            "**Une session Claude Code s'ouvre toujours dans le dossier de développement**",
            "git commit",
        ):
            with self.subTest(said=said):
                self.assertIn(said, words)
        # Its steps in the order deploy.cmd runs them.
        described = tenth.split("Ce que fait `deploy.cmd`", 1)[1].split("\n### ", 1)[0]
        steps = [
            described.index(step)
            for step in (
                "manage.py running_jobs",
                "Arrête le serveur",
                "manage.py backup_data",
                "git merge --ff-only origin/main",
                "uv sync --locked --no-dev",
                "manage.py migrate_tenants",
                "manage.py serve --verifier",
                "Relance le serveur",
            )
        ]
        self.assertEqual(steps, sorted(steps))

    def test_production_installs_with_uv(self):
        """Wherever DEPLOY.md installs the dependencies - the production
        copy made, the way back - it is what deploy.cmd runs. No command
        installs with pip any more, and the way back says a version from
        before uv is not gone back to (the owner, 01/10/2026): its
        deploy.cmd would fail the next deployment on the requirements.txt
        the code no longer has. pip is only QUOTED, in 10.6, where that
        failure is told."""
        tenth = section(self.deploy, "## 10. Développer et mettre en ligne une modification")
        setup = tenth.split("### 10.1 ", 1)[1].split("\n### ", 1)[0]
        back = tenth.split("### 10.4 ", 1)[1].split("\n### ", 1)[0]
        made = [" ".join(block.split()) for block in code_blocks(setup) if "git clone" in block]
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].endswith(f"cd /d C:\\MarginMate\\app mise install {SYNC}"), made[0])
        reset = [" ".join(block.split()) for block in code_blocks(back) if "git reset" in block]
        self.assertEqual(reset, [f"cd /d C:\\MarginMate\\app git reset --hard <version d'avant> {SYNC}"])
        self.assertEqual([block for block in code_blocks(self.deploy) if PIP.search(block) or "ensurepip" in block], [])
        self.assertNotIn("ensurepip", self.deploy)
        self.assertIsNone(PIP.search(back))
        self.assertIn("On ne revient pas avant le passage à uv (section 10.6)", " ".join(back.split()))
        switch = section(self.deploy, "\n### 10.6 Passage à uv (une seule fois)\n")
        self.assertIsNone(PIP.search(self.deploy.replace(switch, "")))

    def test_the_switch_to_uv_is_told_once(self):
        """10.6, what deploy.cmd's refusal points at: the first deployment
        of the uv version, 05a80b4, is run by the PREVIOUS deploy.cmd (pip,
        requirements.txt) and asks nothing; uv must answer in
        C:\\MarginMate\\app before the second, from which deploy.cmd runs uv
        and refuses, before touching anything, when it does not."""
        switch = section(self.deploy, "\n### 10.6 Passage à uv (une seule fois)\n")
        words = " ".join(switch.split())
        for said in (
            "winget install jdx.mise",
            "**La première mise en ligne** est celle de la version `05a80b4`",
            "`deploy.cmd` d'avant",
            "`requirements.txt`",
            "**nouvelle** invite de commandes",
            "cd /d C:\\MarginMate\\app mise install uv --version",
            "« trust »",
            "**À partir de la deuxième mise en ligne**",
            f"`{SYNC}`",
            "« REFUS : uv ne repond pas »",
            "`requirements.txt` a disparu du code après `05a80b4`",
            "On ne revient donc pas à une version d'avant le passage à uv (section 10.4)",
        ):
            with self.subTest(said=said):
                self.assertIn(said, words)
        self.assertLess(words.index("**La première mise en ligne**"), words.index("uv --version"))
        self.assertLess(words.index("uv --version"), words.index("**À partir de la deuxième mise en ligne**"))

    def test_a_production_left_before_uv_is_finished_by_hand(self):
        """The code has no requirements.txt: a production still before
        05a80b4 is updated by ITS deploy.cmd, which fails on pip past the
        merge (the backup made, the server stopped). What 10.6 says it
        prints is what that deploy.cmd prints, and the way forward is what
        this version's deploy.cmd runs after its merge, in its order, then
        the mark that deploy.cmd left removed and the server restarted."""
        switch = section(self.deploy, "\n### 10.6 Passage à uv (une seule fois)\n")
        words = " ".join(switch.split())
        # 79b13b6's deploy.cmd: « echo ECHEC pendant %MM_ETAPE%. » with
        # MM_ETAPE « l'installation des dependances (pip install -r requirements.txt) ».
        self.assertIn(
            "« ECHEC pendant l'installation des dependances (pip install -r requirements.txt) »,"
            " le code déjà mis à jour, les données sauvegardées et le serveur arrêté",
            words,
        )
        self.assertIn("pas de fichier `mise.toml` dans `C:\\MarginMate\\app`", words)
        self.assertIn("Ne revenez pas en arrière : faites l'étape 3", words)
        (forward,) = [block for block in code_blocks(switch) if "migrate_tenants" in block]
        commands = [line.strip() for line in forward.strip().splitlines()]
        self.assertEqual(
            commands,
            [
                SYNC,
                ".venv\\Scripts\\python.exe manage.py migrate_tenants",
                ".venv\\Scripts\\python.exe manage.py serve --verifier",
                "rmdir /s /q .git\\marginmate-deploy",
                "schtasks /run /tn MarginMate",
            ],
        )
        script = Script("deploy.cmd")
        run = [
            script.line_of(needle)
            for needle in ("call uv sync --locked --no-dev", "manage.py migrate_tenants", "manage.py serve --verifier")
        ]
        self.assertEqual(run, sorted(run))
        script.line_of('set "MM_LOCK=%MM_APP%.git\\marginmate-deploy"')
        script.line_of('schtasks /run /tn "%MM_TASK%"')
        self.assertEqual(script.lines[script.line_of('set "MM_TASK=')], 'set "MM_TASK=MarginMate"')
        # The same PATH line as README.md's first-time setup.
        readme = (BASE / "README.md").read_text(encoding="utf-8")
        (path_line,) = [line.strip() for line in readme.splitlines() if "mise\\shims" in line]
        self.assertIn(path_line, switch)
        # What the refusal says is what the owner is told to look for.
        self.assertTrue(
            any(line.startswith("echo REFUS : uv ne repond pas.") for line in Script("deploy.cmd").section("no_uv"))
        )

    def test_the_task_and_the_backups_are_production_s(self):
        seventh = section(self.deploy, "## 7. Démarrer le serveur à l'ouverture de la session")
        self.assertIn("Programme : `C:\\MarginMate\\app\\start_production.cmd`", seventh)
        self.assertIn("« Commencer dans » : `C:\\MarginMate\\app`", seventh)
        eighth = section(self.deploy, "## 8. Sauvegardes")
        self.assertIn("cd /d C:\\MarginMate\\app\n.venv\\Scripts\\python.exe manage.py backup_data", eighth)
        self.assertIn("C:\\MarginMate\\backups", eighth)
        self.assertIn("Gardez plusieurs", eighth)
        self.assertIn("-INCOMPLET", eighth)

    def test_the_review_of_the_scripts_is_told_to_the_owner(self):
        """What deploy.cmd now refuses, and what to do about it, is said
        where the owner reads it."""
        tenth = " ".join(section(self.deploy, "## 10. Développer et mettre en ligne une modification").split())
        for said in (
            "C:\\MarginMate\\app\\.git\\marginmate-deploy",
            "**la marque reste**",
            "rmdir /s /q C:\\MarginMate\\app\\.git\\marginmate-deploy",
            "**seulement si aucune autre fenêtre `deploy.cmd` n'est ouverte**",
            "le site est hors ligne",
            "`MarginMate` lance bien `C:\\MarginMate\\app\\start_production.cmd`",
            "entre la vérification et l'arrêt",
            "git config --global --add safe.directory",
            "`MARGINMATE_ACCOUNTS_DB` doit être `…\\data-dev\\accounts.sqlite3`",
            "une sauvegarde coupée est passée",
        ):
            with self.subTest(said=said):
                self.assertIn(said, tenth)
        eighth = " ".join(section(self.deploy, "## 8. Sauvegardes").split())
        self.assertIn("« fichier(s) disparu(s) pendant la copie »", eighth)
        self.assertIn("n'a pas de `manifest.json`", eighth)
        eleventh = section(self.deploy, "## 11. Quand le serveur refuse de démarrer")
        self.assertIn("« fermé, sans base - ignoré »", eleventh)

    def test_the_credentials_page_and_what_no_backup_holds_are_told_to_the_owner(self):
        """Security review of 01/10/2026: the passwords typed on
        « Identifiants » are in no backup and no development copy, sealed
        for this PC's Windows account; the .env lines go once typed; the
        folder is kept to the owner's account."""
        twelfth = " ".join(section(self.deploy, "## 12. Les identifiants des comptes").split())
        for said in (
            "page « Identifiants »",
            "**ne sont dans aucune sauvegarde**",
            "près une restauration",
            "autre PC",
            "autre compte Windows",
            "DJANGO_SECRET_KEY",
            "supprimez du `.env` de production",
            "`METRO_PASSWORD`",
            "`INVOICE_EMAIL_APP_PASSWORD`",
            "`LADDITION_PASSWORD`",
            (
                'icacls C:\\MarginMate /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" '
                '"*S-1-5-32-544:(OI)(CI)F"'
            ),
            "icacls C:\\MarginMate\\data",
            "compte Windows réservé",
        ):
            with self.subTest(said=said):
                self.assertIn(said, twelfth)
        eighth = " ".join(section(self.deploy, "## 8. Sauvegardes").split())
        for said in ("page « Identifiants »", "sessions de connexion", "`_debug`", "section 12"):
            with self.subTest(said=said):
                self.assertIn(said, eighth)
        refresh = self.deploy.split("### 10.5 Rafraîchir les données de développement", 1)[1].split("\n### ", 1)[0]
        refresh = " ".join(refresh.split())
        for said in ("page « Identifiants »", "sessions de connexion", "`_debug`", "même `DJANGO_SECRET_KEY`"):
            with self.subTest(said=said):
                self.assertIn(said, refresh)

    def test_what_to_do_once_after_this_version_is_told_to_the_owner(self):
        """Second review of « Identifiants » (01/10/2026): the backups and the
        data-dev folders made before this version still hold the site's
        sessions - a password changed once closes them; the portals' .env
        passwords go nowhere until their site is confirmed; the browser's
        offer to keep a password is refused; and whatever runs under the
        owner's Windows account, Claude Code included, reads the .env and
        the store today."""
        from accounts import credentials

        eighth = " ".join(section(self.deploy, "## 8. Sauvegardes").split())
        for said in (
            "**Une fois, après la mise en ligne de cette version.**",
            "`data-dev.ancien-<date>`",
            "Changez une fois votre mot de passe MarginMate",
            "ferme toutes les sessions ouvertes avec l'ancien",
            "manage.py changepassword --database accounts <votre adresse e-mail>",
            "chaque autre compte de vos espaces",
            "`accounts.sqlite3.bak_…`",
        ):
            with self.subTest(section=8, said=said):
                self.assertIn(said, eighth)
        self.assertIn(
            "cd /d C:\\MarginMate\\app\n.venv\\Scripts\\python.exe manage.py changepassword --database accounts",
            section(self.deploy, "## 8. Sauvegardes"),
        )
        refresh = self.deploy.split("### 10.5 Rafraîchir les données de développement", 1)[1].split("\n### ", 1)[0]
        refresh = " ".join(refresh.split())
        for said in ("chaque base de la copie", "ni effacé ni nettoyé", "Supprimez-le dès qu'il ne sert plus"):
            with self.subTest(section="10.5", said=said):
                self.assertIn(said, refresh)
        twelfth = " ".join(section(self.deploy, "## 12. Les identifiants des comptes").split())
        for said in (
            f"« {credentials.STATUS_LABELS[credentials.ENV_UNCONFIRMED]} »",
            "« Le fichier .env contient ces identifiants : les envoyer à … »",
            "une case par espace client",
            "s'arrête, avec un message qui le dit",
            "Votre navigateur peut proposer d'enregistrer les mots de passe",
            "**refusez**",
            "**Claude Code et des sessions de programmation**",
            "peut lire le `.env` de production et les identifiants",
            "**Mieux encore : un compte Windows réservé au serveur.**",
        ):
            with self.subTest(section=12, said=said):
                self.assertIn(said, twelfth)
        self.assertLess(twelfth.index("Claude Code et des sessions"), twelfth.index("**Mieux encore"))

    def test_the_env_copy_and_the_old_kept_pages_are_told_once(self):
        """A copy of a .env left in production's data folder went into every
        backup and into data-dev; the scrapers' pages kept before this
        version were not masked. How to find a copy, what to delete - and
        the key to change when the copy has it - is said once, in section
        12, which section 8 and 10.5 point at."""
        twelfth = section(self.deploy, "## 12. Les identifiants des comptes")
        once = twelfth.split("\n### Une fois, après la mise en ligne de cette version\n", 1)[1].split("\n### ", 1)[0]
        words = " ".join(once.split())
        for said in (
            "`C:\\MarginMate\\data\\.env.bak_…`",
            "Supprimez chaque copie du `.env` rangée dans `C:\\MarginMate\\data`",
            "dir /s /b /a-d C:\\MarginMate\\data\\.env*",
            "si elle ne trouve rien, passez au point 2",
            "`data-dev\\.env.…`",
            "`data-dev.ancien-<date>`",
            "supprimez les sauvegardes plus anciennes qui la contiennent",
            "`C:\\MarginMate\\backups\\<date>\\data\\.env.…`",
            "comparez leurs lignes `DJANGO_SECRET_KEY`",
            "**Si c'est la même clé**, changez celle de `C:\\MarginMate\\app\\.env`",
            "tout le monde est déconnecté",
            "la page « Identifiants » redemande les identifiants",
            "Remove-Item C:\\MarginMate\\data\\tenants\\*\\downloads\\*\\_debug -Recurse -Force",
            "Remove-Item C:\\MarginMate\\data\\tenants\\*\\downloads\\test-* -Recurse -Force",
            "sans rien masquer",
            "« Le fichier .env contient ces identifiants : les envoyer à … »",
        ):
            with self.subTest(said=said):
                self.assertIn(said, words)
        # The key is compared before the copy goes, and the copy before the backups.
        self.assertLess(words.index("comparez leurs lignes"), words.index("Supprimez chaque copie du `.env`"))
        self.assertLess(words.index("Faites une nouvelle sauvegarde"), words.index("supprimez les sauvegardes plus"))
        # What backup_data and the refresh now do about it, where the owner reads them.
        eighth = " ".join(section(self.deploy, "## 8. Sauvegardes").split())
        for said in ("Quatre choses ne sont **jamais** dans une sauvegarde", "`.env.bak_…`", "« ATTENTION »"):
            with self.subTest(section=8, said=said):
                self.assertIn(said, eighth)
        self.assertIn("section 12, « Une fois, après la mise en ligne de cette version »", eighth)
        refresh = self.deploy.split("### 10.5 Rafraîchir les données de développement", 1)[1].split("\n### ", 1)[0]
        refresh = " ".join(refresh.split())
        for said in (
            "contiennent une copie du fichier `.env`",
            "Il ne recopie de toute façon jamais un tel fichier",
            "une jonction, un lien, un lecteur substitué ou un nom court",
        ):
            with self.subTest(section="10.5", said=said):
                self.assertIn(said, refresh)
        # The words backup_data prints are the ones the owner is told to look for.
        self.assertTrue(data_backup.ENV_COPY_WARNING.startswith("ATTENTION : "))
        self.assertIn("DEPLOY.md, section 12", data_backup.ENV_COPY_WARNING)

    def test_where_the_marginmate_password_is_asked_again(self):
        """accounts/sudo.py: the owner's password asked again before the
        page, a portal source saved or tested, a « Données » import or
        clear, the admin. Said in one list, in section 12."""
        twelfth = " ".join(section(self.deploy, "## 12. Les identifiants des comptes").split())
        asked = twelfth.split("**Le site vous redemande votre mot de passe MarginMate**", 1)[1].split(
            "Sur la page « Identifiants » elle-même", 1
        )[0]
        for said in (
            "un quart d'heure",
            "la page « Identifiants »",
            "enregistrer ou « Tester » une source « Espace client »",
            "sur la page « Données », importer une archive et effacer (exporter ne le demande pas)",
            "l'administration du site (`/admin/`)",
        ):
            with self.subTest(said=said):
                self.assertIn(said, asked)

    def test_the_portals_and_the_mailbox_still_in_the_env(self):
        """A portal's .env values go nowhere until its site is confirmed: the
        box, or BOTH its login and its password typed on the page - the
        password alone is not enough. Then its lines leave the .env, with the
        mailbox's under their older names too; « encore en clair » goes once
        the server has restarted."""
        from accounts import credentials

        twelfth = " ".join(section(self.deploy, "## 12. Les identifiants des comptes").split())
        self.assertIn(f"« {credentials.STATUS_LABELS[credentials.ENV_UNCONFIRMED]} »", twelfth)
        for said in (
            "**au choix** : cochez sa case « Le fichier .env contient ces identifiants : les envoyer à … »",
            (
                "**ou** tapez sur la page son identifiant **et** son mot de passe, les deux (le mot de passe seul "
                "ne suffit pas)"
            ),
            "`UBA_EMAIL_ADDRESS` et `UBA_EMAIL_APP_PASSWORD` : supprimez-les aussi",
            "« encore en clair »",
            "L'avertissement disparaît au redémarrage.",
        ):
            with self.subTest(said=said):
                self.assertIn(said, twelfth)
        # The mailbox's older names are the ones the settings still read.
        settings_text = (BASE / "config" / "settings.py").read_text(encoding="utf-8")
        for name in ("UBA_EMAIL_ADDRESS", "UBA_EMAIL_APP_PASSWORD"):
            with self.subTest(name=name):
                self.assertIn(f'os.environ.get("{name}"', settings_text)

    def test_production_is_never_the_desktop_folder(self):
        """The Desktop folder is the development copy's: a section about the
        running site naming it sends the owner to edit the wrong .env."""
        for title in (
            "## 5. Le fichier .env",
            "## 6. Premier démarrage",
            "## 7. Démarrer le serveur",
            "## 8. Sauvegardes",
            "## 9. Le journal",
            "## 11. Quand le serveur refuse de démarrer",
            "## 12. Les identifiants des comptes",
        ):
            with self.subTest(section=title):
                self.assertNotIn("Desktop", section(self.deploy, title))
        self.assertIn("C:\\MarginMate\\data\\logs\\marginmate.log", section(self.deploy, "## 9. Le journal"))

    def test_section_5_s_block_names_no_folder(self):
        """accounts/tests/test_production_settings.py runs the settings with
        that block's lines: a data folder named there would be the owner's."""
        for name in deploy_md_lines():
            self.assertFalse(
                name.startswith(("MARGINMATE_TENANTS_ROOT", "MARGINMATE_ACCOUNTS_DB", "MARGINMATE_LOG_DIR"))
            )


class ClaudeNotesTests(SimpleTestCase):
    def test_the_two_copies_are_in_the_notes(self):
        claude = (BASE / "CLAUDE.md").read_text(encoding="utf-8")
        notes = " ".join(section(claude, "## Two copies: development and production").split())
        for said in (
            "C:\\MarginMate\\app",
            "C:\\MarginMate\\data",
            "C:\\MarginMate\\backups",
            "data-dev",
            "**A coding session edits this folder only and never touches `C:\\MarginMate`**",
            "deploy.cmd",
            "launch.json",
            "never into a fixture",
            "no integration credentials",
            "never run deploy.cmd or refresh_dev_data.cmd",
            "**deploy.cmd runs once at a time, and remembers a run left half way**",
            "mkdir .git\\marginmate-deploy",
            "an accounts database outside the data folder",
        ):
            with self.subTest(said=said):
                self.assertIn(said, notes)
