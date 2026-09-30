import argparse
import asyncio
import ctypes
from dataclasses import dataclass
import os
import re
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# Enable ANSI escape sequences on Windows 10+
if os.name == 'nt':
    os.system('')

from .client import (
    CLEAR_MARKER,
    CloudBridge,
    CloudBridgeConfig,
    CloudBridgeError,
)

SOURCE_LABELS = {
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
    "claude": "Claude",
}
SOURCE_GET_LAST_COMMANDS = {
    "chatgpt": "ai --get-last --chatgpt",
    "gemini": "ai --get-last --gemini",
    "claude": "ai --get-last --claude",
}
DEFAULT_WS_URL = os.getenv("AIBRIDGE_WS_URL", "ws://127.0.0.1:8765")
DEFAULT_HTTP_URL = os.getenv("AIBRIDGE_HTTP_URL", "http://127.0.0.1:8766")
RAW_CLI_MODULE_RE = re.compile(
    r'(?<!\S)-m\s+(?:"aibridge\.(?:cli|command)"|aibridge\.(?:cli|command))(?=\s|$)',
    re.IGNORECASE,
)
VALUE_OPTIONS = {"--source", "--file", "--url", "--health-url"}


@dataclass(frozen=True)
class RawCliToken:
    value: str
    quote: str | None


@dataclass(frozen=True)
class TextInvocation:
    inline_prompt: str | None = None
    is_debate: bool = False


class CliSyntaxError(ValueError):
    pass


def get_raw_cli_arguments() -> str | None:
    """Return the untouched argument tail used to start this Windows process."""
    if os.name != "nt":
        return None

    try:
        get_command_line = ctypes.windll.kernel32.GetCommandLineW
        get_command_line.restype = ctypes.c_wchar_p
        command_line = get_command_line()
    except (AttributeError, OSError):
        return None

    if not command_line:
        return None
    module_match = RAW_CLI_MODULE_RE.search(command_line)
    if not module_match:
        return None
    return command_line[module_match.end():].lstrip()


def tokenize_raw_cli_arguments(raw_arguments: str) -> list[RawCliToken]:
    """Split the CLI tail while treating both quote styles as delimiters."""
    tokens: list[RawCliToken] = []
    index = 0
    length = len(raw_arguments)

    while index < length:
        while index < length and raw_arguments[index].isspace():
            index += 1
        if index >= length:
            break

        token_start = index
        outer_quote = raw_arguments[index] if raw_arguments[index] in {'"', "'"} else None
        active_quote: str | None = None
        outer_quote_closed = False
        outer_quote_only = outer_quote is not None
        value: list[str] = []
        while index < length:
            character = raw_arguments[index]

            if active_quote is not None:
                if character == "\\" and index + 1 < length and raw_arguments[index + 1] == active_quote:
                    value.append(active_quote)
                    index += 2
                    continue
                if character == active_quote:
                    if active_quote == outer_quote and outer_quote_only:
                        outer_quote_closed = True
                    active_quote = None
                    index += 1
                    continue
                value.append(character)
                index += 1
                continue

            if character.isspace():
                break

            quote_starts_group = character == '"' or (
                character == "'"
                and (index == token_start or (value and value[-1] == "="))
            )
            if quote_starts_group:
                if index != token_start:
                    outer_quote_only = False
                active_quote = character
                index += 1
                continue

            if outer_quote_closed:
                outer_quote_only = False
            value.append(character)
            index += 1

        if active_quote is not None:
            raise CliSyntaxError(
                "Ошибка: обнаружена незакрытая кавычка; запрос не отправлен."
            )
        quote = outer_quote if outer_quote_only and outer_quote_closed else None
        tokens.append(RawCliToken("".join(value), quote))

    return tokens


def clean_source_text(text: str, source: str | None = None) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    hidden_prefixes = ["Ответ Gemini", "Gemini response"]
    if source == "gemini":
        for prefix in hidden_prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
    return cleaned


def normalize_source(source: str) -> str:
    return source.strip().casefold()


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(normalize_source(source), source)


def get_last_command(source: str) -> str:
    normalized = normalize_source(source)
    return SOURCE_GET_LAST_COMMANDS.get(normalized, f"ai --get-last --source {source}")


def format_source_list(sources: list[str]) -> str:
    return ", ".join(source_label(source) for source in sources)


def print_completion_summary(
    sources: list[str],
    missing_sources: list[tuple[str, BaseException]] | None = None,
) -> None:
    missing_sources = missing_sources or []
    if missing_sources:
        print(f"Получены ответы: {format_source_list(sources)}.")
        print("Не получены ответы:")
        for source, error in missing_sources:
            print(f"  {source_label(source)}: {error}")
    else:
        print(f"Все ответы получены: {format_source_list(sources)}.")

    print("Чтобы прочитать полученные ответы, используйте get-last отдельно для каждой модели:")
    for source in sources:
        print(f"  {get_last_command(source)}")


def print_get_last_usage_error() -> None:
    print(
        "Ошибка: ai --get-last без ровно одной модели отключен. "
        "Используйте: ai --get-last --chatgpt, ai --get-last --gemini или ai --get-last --claude.",
        file=sys.stderr,
    )


def token_text(token: str) -> str:
    if token.startswith("\0CLEAR\0"):
        return token[7:]
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a prompt to browser-based AI through AI Bridge.",
        allow_abbrev=False,
    )
    parser.add_argument("prompt", type=str, nargs="*", help="The prompt to send to the AI")
    parser.add_argument("--source", type=str, default=None, help="Specific source to target (legacy)")
    parser.add_argument("--chatgpt", action="store_true", help="Send to ChatGPT")
    parser.add_argument("--gemini", action="store_true", help="Send to Gemini")
    parser.add_argument("--claude", action="store_true", help="Send to Claude")
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Read the prompt text from a UTF-8 file",
    )
    parser.add_argument("--get-last", action="store_true", help="Get the last message from exactly one selected source")
    parser.add_argument("--status", action="store_true", help="Start the bridge if needed and show connected sources")
    parser.add_argument("--url", type=str, default=DEFAULT_WS_URL, help="WebSocket URL of the broker")
    parser.add_argument(
        "--health-url",
        type=str,
        default=DEFAULT_HTTP_URL,
        help="HTTP URL used for health and active-source checks",
    )
    return parser


def target_sources_from_args(args: argparse.Namespace) -> list[str]:
    target_sources = []
    if args.chatgpt:
        target_sources.append("chatgpt")
    if args.gemini:
        target_sources.append("gemini")
    if args.claude:
        target_sources.append("claude")
    if args.source:
        target_sources.append(normalize_source(args.source))
    return target_sources


def validate_get_last_args(args: argparse.Namespace) -> int | None:
    if not args.get_last:
        return None
    if args.file or args.prompt:
        print("Ошибка: --get-last нельзя сочетать с текстом промта или --file.", file=sys.stderr)
        return 1
    if len(target_sources_from_args(args)) != 1:
        print_get_last_usage_error()
        return 1
    return None


def positional_raw_tokens(tokens: list[RawCliToken]) -> list[RawCliToken]:
    positionals: list[RawCliToken] = []
    options_finished = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if options_finished:
            positionals.append(token)
            index += 1
            continue

        if token.quote is None and token.value == "--":
            options_finished = True
            index += 1
            continue

        if token.quote is None and token.value.startswith("--"):
            option_name, separator, _option_value = token.value.partition("=")
            if option_name in VALUE_OPTIONS and not separator:
                index += 2
            else:
                index += 1
            continue

        positionals.append(token)
        index += 1

    return positionals


def resolve_text_invocation(
    args: argparse.Namespace,
    raw_tokens: list[RawCliToken] | None,
) -> TextInvocation:
    if args.status or args.get_last:
        return TextInvocation()

    if raw_tokens is None:
        if args.prompt:
            raise CliSyntaxError(
                "Ошибка: невозможно проверить кавычки исходной команды. "
                "Запускайте запрос через ai.cmd; запрос не отправлен."
            )
        if args.file:
            return TextInvocation()
        raise CliSyntaxError(
            "Ошибка: текст запроса не указан. Используйте текст в кавычках или --file <путь>."
        )

    positionals = positional_raw_tokens(raw_tokens)
    first_is_debate = bool(
        positionals
        and positionals[0].quote is None
        and positionals[0].value.casefold() == "debate"
    )

    if args.file:
        if not positionals:
            return TextInvocation()
        if first_is_debate and len(positionals) == 1:
            return TextInvocation(is_debate=True)
        raise CliSyntaxError(
            "Ошибка: укажите текст промта или --file, но не оба одновременно; запрос не отправлен."
        )

    if first_is_debate:
        if len(positionals) != 2 or positionals[1].quote not in {'"', "'"}:
            raise CliSyntaxError(
                "Ошибка: тема дебатов должна быть целиком в одинарных или двойных кавычках. "
                "Пример: ai debate \"тема\". Запрос не отправлен."
            )
        topic = positionals[1].value
        if not topic.strip():
            raise CliSyntaxError("Ошибка: тема для дебатов пуста; запрос не отправлен.")
        return TextInvocation(inline_prompt=topic, is_debate=True)

    if len(positionals) != 1 or positionals[0].quote not in {'"', "'"}:
        raise CliSyntaxError(
            "Ошибка: текстовый промпт должен быть одним аргументом в одинарных или двойных кавычках. "
            "Правильно: ai \"hello\" или ai 'hello'. Без кавычек запрос не отправлен."
        )

    prompt = positionals[0].value
    if not prompt.strip():
        raise CliSyntaxError("Ошибка: текст запроса пуст; запрос не отправлен.")
    return TextInvocation(inline_prompt=prompt)


def parse_command_args(
    argv: list[str] | None = None,
    raw_arguments: str | None = None,
) -> tuple[argparse.Namespace, list[RawCliToken] | None]:
    raw_tokens = (
        tokenize_raw_cli_arguments(raw_arguments)
        if raw_arguments is not None
        else None
    )
    parser_argv = [token.value for token in raw_tokens] if raw_tokens is not None else argv
    return build_parser().parse_args(parser_argv), raw_tokens


def validate_parsed_args(
    args: argparse.Namespace,
    raw_tokens: list[RawCliToken] | None,
) -> tuple[int | None, TextInvocation | None]:
    get_last_code = validate_get_last_args(args)
    if get_last_code is not None:
        return get_last_code, None

    if args.status:
        if args.file or args.prompt or target_sources_from_args(args) or args.get_last:
            print("Ошибка: --status нельзя сочетать с промптом или выбором модели.", file=sys.stderr)
            return 1, None
        return None, TextInvocation()

    try:
        return None, resolve_text_invocation(args, raw_tokens)
    except CliSyntaxError as exc:
        print(str(exc), file=sys.stderr)
        return 2, None


def validate_without_bridge(
    argv: list[str] | None = None,
    raw_arguments: str | None = None,
) -> int | None:
    try:
        args, raw_tokens = parse_command_args(argv, raw_arguments)
    except CliSyntaxError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)
    validation_code, _invocation = validate_parsed_args(args, raw_tokens)
    return validation_code


async def wait_for_active_sources(
    bridge: CloudBridge,
    attempts: int = 40,
    delay: float = 0.5,
    required_source: str | None = None,
) -> list[str]:
    for _ in range(attempts):
        sources = await bridge.get_active_sources()
        if sources and (required_source is None or required_source in sources):
            return sources
        await asyncio.sleep(delay)
    return []

async def main(raw_arguments: str | None = None):
    try:
        args, raw_tokens = parse_command_args(sys.argv[1:], raw_arguments)
    except CliSyntaxError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    validation_code, invocation = validate_parsed_args(args, raw_tokens)
    if validation_code is not None:
        sys.exit(validation_code)
    assert invocation is not None

    if args.status:
        bridge = CloudBridge(
            CloudBridgeConfig(url=args.url, health_url=args.health_url)
        )
        sources = await bridge.get_active_sources()
        source_text = format_source_list(sources) if sources else "нет подключённых AI-вкладок"
        print(f"AI Bridge работает; {source_text}.")
        return

    if args.get_last:
        source = target_sources_from_args(args)[0]
        bridge = CloudBridge(
            CloudBridgeConfig(url=args.url, health_url=args.health_url)
        )
        sources = await wait_for_active_sources(bridge, required_source=source)
        if source not in sources:
            print(
                f"{source_label(source)}: открытая вкладка с подключённым расширением не найдена.",
                file=sys.stderr,
            )
            sys.exit(1)
        text = ""
        try:
            async for token in bridge.stream_current_tab(source):
                chunk = token_text(token)
                if token.startswith(CLEAR_MARKER):
                    text = chunk
                else:
                    text += chunk
        except CloudBridgeError as exc:
            print(f"{source_label(source)}: ошибка: {exc}", file=sys.stderr)
            sys.exit(1)

        cleaned = clean_source_text(text, source)
        if not cleaned.strip():
            print(f"{source_label(source)}: в открытой вкладке ответ не найден.", file=sys.stderr)
            sys.exit(1)
        print(cleaned)
        return

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as file:
                prompt = file.read()
        except Exception as e:
            print(f"Ошибка при чтении файла {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        assert invocation.inline_prompt is not None
        prompt = invocation.inline_prompt

    is_debate = invocation.is_debate
    debate_topic = prompt if is_debate else ""

    config = CloudBridgeConfig(url=args.url, health_url=args.health_url)
    bridge = CloudBridge(config)

    target_sources = target_sources_from_args(args)

    try:
        if is_debate:
            from .debate_orchestrator import run_debate_cli
            await run_debate_cli(debate_topic, bridge)
            return

        if not target_sources:
            target_sources = await wait_for_active_sources(bridge)
            if not target_sources:
                print(
                    "Ошибка: AI Bridge запущен, но расширение не сообщило ни одной AI-вкладки. "
                    "Откройте ChatGPT/Gemini/Claude и дождитесь подключения расширения.",
                    file=sys.stderr,
                )
                sys.exit(1)
    except Exception as e:
        print(f"Ошибка при получении списка AI-вкладок: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        async def collect_source(source):
            saw_text = False
            try:
                async for token in bridge.stream_completion(prompt, source=source):
                    chunk = token_text(token)
                    if chunk.strip():
                        saw_text = True
                if not saw_text:
                    raise RuntimeError("ответ не получен")
                return source, None
            except Exception as e:
                return source, e

        if len(target_sources) == 1:
            source, err = await collect_source(target_sources[0])
            if err:
                print(f"{source_label(source)}: ошибка: {err}", file=sys.stderr)
                sys.exit(1)
            print_completion_summary([source])
        else:
            # Multi-source mode: fetch concurrently but keep model outputs hidden.
            tasks = [asyncio.create_task(collect_source(s)) for s in target_sources]
            results = await asyncio.gather(*tasks)
            successful_sources = []
            missing_sources = []

            for source, err in results:
                if err:
                    missing_sources.append((source, err))
                else:
                    successful_sources.append(source)

            if not successful_sources:
                for source, err in missing_sources:
                    print(f"{source_label(source)}: ошибка: {err}", file=sys.stderr)
                sys.exit(1)

            print_completion_summary(successful_sources, missing_sources)

    except Exception as e:
        print(f"\nОшибка: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main(raw_arguments=get_raw_cli_arguments()))
    except KeyboardInterrupt:
        pass
