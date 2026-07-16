import argparse
import asyncio
import sys
import os
import re

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# Enable ANSI escape sequences on Windows 10+
if os.name == 'nt':
    os.system('')

from .client import CloudBridge, CloudBridgeConfig

SECTION_HEADING_RE = re.compile(r"^[\wА-Яа-яЁё][\wА-Яа-яЁё\s/&,+-]{1,64}:?$")
LEADING_BULLET_RE = re.compile(r"^\s*(?:[-*•◦○●]|\d+[.)])\s+")
ACTION_STARTS = {
    "анализировать", "выполнять", "готовить", "давать", "делать", "искать",
    "извлекать", "обрабатывать", "объяснять", "обсуждать", "организовывать",
    "отвечать", "переводить", "писать", "планировать", "помогать",
    "проверять", "работать", "разбирать", "редактировать", "решать",
    "сравнивать", "создавать", "составлять", "строить", "улучшать",
}
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


def format_response_text(text: str) -> str:
    lines = [line for line in normalize_response_lines(text) if line]
    if not lines:
        return ""

    output: list[str] = []
    in_section = False
    index = 0

    while index < len(lines):
        line = lines[index]
        next_line = lines[index + 1] if index + 1 < len(lines) else ""

        if is_section_heading(line, next_line):
            output.append(line.rstrip(":") + ":")
            in_section = True
            index += 1
            continue

        if in_section and line:
            output.append("  • " + strip_existing_bullet(line))
        else:
            output.append(line)
        index += 1

    return "\n".join(trim_blank_edges(collapse_blank_lines(output)))


def normalize_response_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    lines: list[str] = []

    for line in raw_lines:
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        lines.append(line)

    return trim_blank_edges(lines)


def is_section_heading(line: str, next_line: str) -> bool:
    if not line or not next_line:
        return False
    if LEADING_BULLET_RE.match(line):
        return False
    if len(line) > 72:
        return False
    if line.endswith((".", "!", "?", "…", ",")):
        return False
    if ":" in line and not line.endswith(":"):
        return False
    if is_action_line(line):
        return False
    word_count = len(line.rstrip(":").split())
    return bool(SECTION_HEADING_RE.match(line.rstrip(":"))) and (line.endswith(":") or word_count <= 5)


def is_action_line(line: str) -> bool:
    first_word = line.rstrip(":").split(maxsplit=1)[0].casefold() if line.split() else ""
    return first_word in ACTION_STARTS


def strip_existing_bullet(line: str) -> str:
    return LEADING_BULLET_RE.sub("", line).strip()


def collapse_blank_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    for line in lines:
        if not line and (not collapsed or not collapsed[-1]):
            continue
        collapsed.append(line)
    return collapsed


def trim_blank_edges(lines: list[str]) -> list[str]:
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def clean_source_text(text: str, source: str | None = None) -> str:
    cleaned = text.strip()
    hidden_prefixes = ["Ответ Gemini", "Gemini response"]
    if source == "gemini":
        for prefix in hidden_prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
    return format_response_text(cleaned)


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
    parser = argparse.ArgumentParser(description="Send a prompt to browser-based AI through AI Bridge.")
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


def validate_without_bridge(argv: list[str] | None = None) -> int | None:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return validate_get_last_args(args)


async def wait_for_active_sources(bridge: CloudBridge, attempts: int = 40, delay: float = 0.5) -> list[str]:
    for _ in range(attempts):
        sources = await bridge.get_active_sources()
        if sources:
            return sources
        await asyncio.sleep(delay)
    return []

async def main():
    args = build_parser().parse_args()
    get_last_validation_code = validate_get_last_args(args)
    if get_last_validation_code is not None:
        sys.exit(get_last_validation_code)

    if args.status:
        if args.file or args.prompt or target_sources_from_args(args) or args.get_last:
            print("Ошибка: --status нельзя сочетать с промптом или выбором модели.", file=sys.stderr)
            sys.exit(1)
        bridge = CloudBridge(
            CloudBridgeConfig(url=args.url, health_url=args.health_url)
        )
        sources = await bridge.get_active_sources()
        source_text = format_source_list(sources) if sources else "нет подключённых AI-вкладок"
        print(f"AI Bridge работает; {source_text}.")
        return

    if args.get_last:
        prompt = ""
    elif args.file:
        if args.prompt and args.prompt[0].lower() != "debate":
            print("Ошибка: укажите текст промта или --file, но не оба одновременно.", file=sys.stderr)
            sys.exit(1)

        try:
            with open(args.file, "r", encoding="utf-8") as file:
                prompt = file.read()
        except Exception as e:
            print(f"Ошибка при чтении файла {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        prompt = " ".join(args.prompt)
        if not prompt:
            print("Ошибка: текст запроса не указан. Используйте текст или --file <путь>.", file=sys.stderr)
            sys.exit(1)

    is_debate = False
    debate_topic = ""
    if args.prompt and args.prompt[0].lower() == "debate":
        is_debate = True
        if args.file:
            if len(args.prompt) != 1:
                print("Ошибка: для дебатов укажите тему или --file, но не оба одновременно.", file=sys.stderr)
                sys.exit(1)
            debate_topic = prompt
        else:
            debate_topic = " ".join(args.prompt[1:])
        if not debate_topic:
            print("Ошибка: тема для дебатов не указана. Используйте: ai debate <тема> или ai debate --file <путь>", file=sys.stderr)
            sys.exit(1)
        prompt = debate_topic

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
        async def collect_source(source, capture_text: bool):
            text = ""
            saw_text = False
            try:
                async for token in bridge.stream_completion(prompt, source=source):
                    chunk = token_text(token)
                    if chunk.strip():
                        saw_text = True
                    if not capture_text:
                        continue
                    if token.startswith("\0CLEAR\0"):
                        text = chunk
                    else:
                        text += chunk

                cleaned = clean_source_text(text, source) if capture_text else ""
                if not saw_text or (capture_text and not cleaned.strip()):
                    raise RuntimeError("ответ не получен")
                return source, cleaned, None
            except Exception as e:
                return source, None, e

        if len(target_sources) == 1:
            source, text, err = await collect_source(target_sources[0], args.get_last)
            if err:
                print(f"{source_label(source)}: ошибка: {err}", file=sys.stderr)
                sys.exit(1)
            elif args.get_last:
                print(text)
            else:
                print_completion_summary([source])
        else:
            # Multi-source mode: fetch concurrently but keep model outputs hidden.
            tasks = [asyncio.create_task(collect_source(s, False)) for s in target_sources]
            results = await asyncio.gather(*tasks)
            successful_sources = []
            missing_sources = []

            for source, _text, err in results:
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
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
