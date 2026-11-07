from __future__ import annotations

import ast
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_WRITER_PATH = REPO_ROOT / "aibridge" / "report_writer.py"
PYTHON_SOURCE_DIRS = ("aibridge", "tests", "scripts")


class _FrozenDatetime(datetime):
    """Fixed clock so filenames and the frontmatter date are deterministic."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5)


def _python_sources() -> list[Path]:
    files: list[Path] = []
    for name in PYTHON_SOURCE_DIRS:
        directory = REPO_ROOT / name
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def _backslashes_in_fstring_expressions(path: Path) -> list[int]:
    """Line numbers of f-string replacement fields whose expression contains a
    backslash. Python <= 3.11 rejects that with a SyntaxError; 3.12+ (PEP 701)
    accepts it, which is how the original defect slipped in."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue):
            segment = ast.get_source_segment(source, node.value) or ""
            if "\\" in segment:
                lines.append(node.lineno)
    return lines


class SourceCompatibilityTests(unittest.TestCase):
    """Do not import report_writer here: these tests must report a precise
    failure even when the module cannot be imported at all."""

    def test_report_writer_compiles_on_running_interpreter(self) -> None:
        # Fails on the pre-fix file under Python 3.11 (the project baseline).
        try:
            compile(REPORT_WRITER_PATH.read_bytes(), str(REPORT_WRITER_PATH), "exec", dont_inherit=True)
        except SyntaxError as exc:
            self.fail(f"report_writer.py does not compile on Python {sys.version.split()[0]}: {exc}")

    @unittest.skipIf(
        sys.version_info < (3, 12),
        "on Python <= 3.11 compile() itself enforces this rule (see compileall and the test above)",
    )
    def test_no_backslash_in_fstring_expressions(self) -> None:
        # Backstop for developers running the suite on 3.12+, where the
        # compile() test above can no longer see this class of bug.
        offenders = {}
        for path in _python_sources():
            lines = _backslashes_in_fstring_expressions(path)
            if lines:
                offenders[str(path.relative_to(REPO_ROOT))] = lines
        self.assertEqual(offenders, {}, "f-string expressions with a backslash break Python 3.11")


class ReportWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Imported here (not at module level) so that a SyntaxError in
        # report_writer.py shows up as an error of this class while the
        # SourceCompatibilityTests above still run and explain the cause.
        cls.rw = importlib.import_module("aibridge.report_writer")
        cls.DebateState = importlib.import_module("aibridge.debate_state").DebateState

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A not-yet-existing subdirectory also exercises ensure_reports_dir().
        self.reports_dir = os.path.join(tmp.name, "reports")
        for patcher in (
            mock.patch.object(self.rw, "REPORTS_DIR", self.reports_dir),
            mock.patch.object(self.rw, "datetime", _FrozenDatetime),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _frontmatter(self, path: str) -> list[str]:
        lines = self._read(path).split("\n")
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        return lines[1:end]

    def _full_state(self, topic: str):
        return self.DebateState(
            topic=topic,
            normalized_problem={"goal": "Выбрать архитектуру", "constraints": ["budget"]},
            round_1_positions={"chatgpt": "Монолит.\nПроще деплой.", "claude": "Сервисы."},
            round_2_critiques={"gemini": "Риски роста."},
            synthesis="Начать с модульного монолита.",
            decision_matrix="| Вариант | Оценка |\n|---|---|\n| Монолит | 8 |",
        )

    # --- regression: the frontmatter title (the two lines that broke 3.11) ---

    def test_markdown_title_removes_double_quotes_like_before(self) -> None:
        topic = 'Use "Postgres" or \'SQLite\'?'
        path = self.rw.save_markdown_report(self.DebateState(topic=topic))
        self.assertEqual(
            self._frontmatter(path),
            ["title: \"Use Postgres or 'SQLite'?\"", "date: 2026-01-02 03:04:05", "type: debate"],
        )
        # The visible heading keeps the original topic, quotes included.
        self.assertIn(f"# ⚖️ Дебаты: {topic}\n", self._read(path))

    def test_parallel_title_removes_double_quotes_like_before(self) -> None:
        path = self.rw.save_parallel_report('Say "hi"', {}, "syn")
        self.assertEqual(
            self._frontmatter(path),
            ['title: "Say hi"', "date: 2026-01-02 03:04:05", "type: parallel_consultation"],
        )

    def test_title_with_backslash_and_line_breaks_stays_one_valid_scalar(self) -> None:
        topic = 'C:\\temp\\new "x"\r\nline 2\tend'
        path = self.rw.save_markdown_report(self.DebateState(topic=topic))
        title_line = self._frontmatter(path)[0]
        self.assertEqual(title_line, 'title: "C:\\\\temp\\\\new x\\r\\nline 2\\tend"')
        # A JSON string is a valid YAML double-quoted scalar; decoding it must
        # give back the topic with only the double quotes removed.
        self.assertEqual(json.loads(title_line[len("title: "):]), topic.replace('"', ""))

    # --- golden output: structure and text are unchanged ---

    def test_markdown_report_golden_output(self) -> None:
        state = self._full_state('Choose "an" architecture')
        path = self.rw.save_markdown_report(state)
        self.assertEqual(
            os.path.basename(path), "2026-01-02_03-04-05_debate_choose_an_architecture.md"
        )
        expected = (
            "---\n"
            'title: "Choose an architecture"\n'
            "date: 2026-01-02 03:04:05\n"
            "type: debate\n"
            "---\n\n"
            '# ⚖️ Дебаты: Choose "an" architecture\n\n'
            "## 🎯 1. Формализация проблемы\n\n"
            "<details>\n<summary>Посмотреть структуру (JSON)</summary>\n\n"
            "```json\n"
            '{\n  "goal": "Выбрать архитектуру",\n  "constraints": [\n    "budget"\n  ]\n}'
            "\n```\n"
            "</details>\n\n"
            "---\n\n"
            "## 💡 2. Позиции (Раунд 1)\n\n"
            "### 👤 CHATGPT\n\n> Монолит.\n> Проще деплой.\n\n"
            "### 👤 CLAUDE\n\n> Сервисы.\n\n"
            "---\n\n"
            "## ⚔️ 3. Перекрестная критика (Раунд 2)\n\n"
            "### 🛡️ GEMINI\n\n> Риски роста.\n\n"
            "---\n\n"
            "## 🧠 4. Синтез (Раунд 3)\n\n"
            "Начать с модульного монолита.\n\n"
            "---\n\n"
            "## 📋 5. Матрица решений (Раунд 4)\n\n"
            "| Вариант | Оценка |\n|---|---|\n| Монолит | 8 |\n\n"
        )
        self.assertEqual(self._read(path), expected)

    def test_markdown_report_with_empty_debate_state(self) -> None:
        # Rounds that produced nothing must not crash report writing.
        path = self.rw.save_markdown_report(self.DebateState(topic="Пустые дебаты"))
        content = self._read(path)
        self.assertIn("Не удалось формализовать.", content)
        self.assertIn("## 📋 5. Матрица решений (Раунд 4)", content)

    def test_parallel_report_golden_output_and_json_log(self) -> None:
        responses = {"chatgpt": "Да.\nНо осторожно.", "gemini": "Нет."}
        path = self.rw.save_parallel_report("Rust или Go?", responses, "Итог: Go.")
        self.assertEqual(os.path.basename(path), "2026-01-02_03-04-05_parallel_rust_или_go.md")
        expected = (
            "---\n"
            'title: "Rust или Go?"\n'
            "date: 2026-01-02 03:04:05\n"
            "type: parallel_consultation\n"
            "---\n\n"
            "# ⚡ Параллельная Консультация: Rust или Go?\n\n"
            "---\n\n"
            "## 🧠 Итоговая Сводка (Synthesis)\n\n"
            "Итог: Go.\n\n"
            "---\n\n"
            "## 💬 Ответы консультантов\n\n"
            "<details>\n<summary><b>Развернуть ответ CHATGPT</b></summary>\n\n"
            "> Да.\n> Но осторожно.\n\n</details>\n\n"
            "<details>\n<summary><b>Развернуть ответ GEMINI</b></summary>\n\n"
            "> Нет.\n\n</details>\n\n"
        )
        self.assertEqual(self._read(path), expected)

        json_path = os.path.join(self.reports_dir, "2026-01-02_03-04-05_parallel_rust_или_go.json")
        with open(json_path, encoding="utf-8") as f:
            self.assertEqual(
                json.load(f),
                {
                    "topic": "Rust или Go?",
                    "mode": "parallel",
                    "responses": responses,
                    "synthesis": "Итог: Go.",
                    "timestamp": "2026-01-02_03-04-05",
                },
            )
        self.assertEqual(len(os.listdir(self.reports_dir)), 2)

    # --- the other public helpers ---

    def test_save_json_log_with_debate_state(self) -> None:
        state = self._full_state("Выбор архитектуры")
        path = self.rw.save_json_log(state)
        self.assertEqual(os.path.basename(path), "2026-01-02_03-04-05_debate_выбор_архитектуры.json")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), state.to_dict())
        # ensure_ascii=False keeps Cyrillic readable in the file.
        self.assertIn("Выбор архитектуры", self._read(path))

    def test_save_json_log_with_plain_dict(self) -> None:
        data = {"topic": "plain dict", "value": 1}
        path = self.rw.save_json_log(data, mode="parallel")
        self.assertEqual(os.path.basename(path), "2026-01-02_03-04-05_parallel_plain_dict.json")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), data)

    def test_save_json_log_without_topic_omits_slug(self) -> None:
        path = self.rw.save_json_log({"value": 1})
        self.assertEqual(os.path.basename(path), "2026-01-02_03-04-05_debate.json")

    def test_format_ai_quote(self) -> None:
        self.assertEqual(self.rw.format_ai_quote(""), "")
        self.assertEqual(self.rw.format_ai_quote(None), "")
        self.assertEqual(self.rw.format_ai_quote("a\nb"), "> a\n> b")
        # Current behavior, pinned: a trailing newline yields an empty quote line.
        self.assertEqual(self.rw.format_ai_quote("a\n"), "> a\n> ")

    def test_ensure_reports_dir_is_idempotent(self) -> None:
        self.assertFalse(os.path.exists(self.reports_dir))
        self.rw.ensure_reports_dir()
        self.rw.ensure_reports_dir()
        self.assertTrue(os.path.isdir(self.reports_dir))

    def test_debate_orchestrator_imports_report_writer(self) -> None:
        # The import that made `ai debate` fail before any prompt was sent.
        orchestrator = importlib.import_module("aibridge.debate_orchestrator")
        self.assertIs(orchestrator.save_markdown_report, self.rw.save_markdown_report)
        self.assertIs(orchestrator.save_json_log, self.rw.save_json_log)
        self.assertTrue(callable(orchestrator.run_debate_cli))


if __name__ == "__main__":
    unittest.main()
