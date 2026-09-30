from __future__ import annotations

import io
import sys
import unittest
from unittest import mock

from aibridge import cli, command


class FakeBridge:
    def __init__(self, *_args, **_kwargs) -> None:
        self.current_tab_calls: list[str] = []
        self.completion_calls: list[tuple[str, str | None]] = []
        self.stream_called = False
        self.sources_called = False

    async def stream_current_tab(self, source: str):
        self.current_tab_calls.append(source)
        yield "\0CLEAR\0open tab output"

    async def stream_completion(self, prompt: str, source: str | None = None):
        self.stream_called = True
        self.completion_calls.append((prompt, source))
        yield "ok"

    async def get_active_sources(self) -> list[str]:
        self.sources_called = True
        return ["chatgpt"]


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def test_raw_cli_tokenizer_handles_quotes_and_quoted_option_values(self) -> None:
        tokens = command.tokenize_raw_cli_arguments(
            '"hello my friend" --file="C:\\prompt files\\one.md"'
        )

        self.assertEqual(
            tokens,
            [
                command.RawCliToken("hello my friend", '"'),
                command.RawCliToken("--file=C:\\prompt files\\one.md", None),
            ],
        )

        single_quoted = command.tokenize_raw_cli_arguments(
            "'hello my friend' --source='chatgpt'"
        )
        self.assertEqual(
            single_quoted,
            [
                command.RawCliToken("hello my friend", "'"),
                command.RawCliToken("--source=chatgpt", None),
            ],
        )

    async def test_double_quoted_prompt_is_sent_without_quotes(self) -> None:
        fake_bridge = FakeBridge()
        with (
            mock.patch.object(command, "CloudBridge", return_value=fake_bridge),
            mock.patch.object(sys, "argv", ["ai", "hello my friend", "--chatgpt"]),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            await command.main(raw_arguments='"hello my friend" --chatgpt')

        self.assertEqual(fake_bridge.completion_calls, [("hello my friend", "chatgpt")])

    async def test_single_quoted_prompt_is_sent_without_quotes(self) -> None:
        fake_bridge = FakeBridge()
        with (
            mock.patch.object(command, "CloudBridge", return_value=fake_bridge),
            mock.patch.object(
                sys,
                "argv",
                ["ai", "'hello", "my", "friend'", "--chatgpt"],
            ),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            await command.main(raw_arguments="'hello my friend' --chatgpt")

        self.assertEqual(fake_bridge.completion_calls, [("hello my friend", "chatgpt")])

    async def test_unquoted_prompt_is_rejected_before_bridge_start(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["ai", "hello"]),
            mock.patch.object(cli, "get_raw_cli_arguments", return_value="hello"),
            mock.patch.object(cli, "ensure_bridge") as ensure_bridge,
            mock.patch("sys.stderr", stderr),
        ):
            exit_code = await cli.run()

        self.assertEqual(exit_code, 2)
        ensure_bridge.assert_not_called()
        self.assertIn("Без кавычек запрос не отправлен", stderr.getvalue())

    async def test_multiple_quoted_prompt_arguments_are_rejected(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["ai", "hello", "friend"]),
            mock.patch.object(
                cli,
                "get_raw_cli_arguments",
                return_value='"hello" "friend"',
            ),
            mock.patch.object(cli, "ensure_bridge") as ensure_bridge,
            mock.patch("sys.stderr", stderr),
        ):
            exit_code = await cli.run()

        self.assertEqual(exit_code, 2)
        ensure_bridge.assert_not_called()

    async def test_get_last_reads_open_browser_tab(self) -> None:
        fake_bridge = FakeBridge()
        stdout = io.StringIO()
        with (
            mock.patch.object(command, "CloudBridge", return_value=fake_bridge),
            mock.patch.object(sys, "argv", ["ai", "--get-last", "--chatgpt"]),
            mock.patch("sys.stdout", stdout),
        ):
            await command.main()

        self.assertEqual(stdout.getvalue(), "open tab output\n")
        self.assertEqual(fake_bridge.current_tab_calls, ["chatgpt"])
        self.assertFalse(fake_bridge.stream_called)
        self.assertTrue(fake_bridge.sources_called)

    def test_clean_source_text_preserves_markdown_and_code_indentation(self) -> None:
        original = (
            "The boundary is **C**.\n\n"
            "## Example\n\n"
            "```dart\n"
            "final class Example {\n"
            "  const Example();\n"
            "}\n"
            "```"
        )

        self.assertEqual(command.clean_source_text(original, "chatgpt"), original)


if __name__ == "__main__":
    unittest.main()
