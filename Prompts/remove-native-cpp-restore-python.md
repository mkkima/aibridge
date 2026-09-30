# Безопасно удалить нативный C++ прототип и вернуть Python AI Bridge

## Суть задачи

Пользователь отказался от C++/Win32 `.exe` прототипа из-за лишней сложности. Нужно полностью удалить только нативную ветку и вернуть рабочий Python CLI/брокер без потери уже сделанных Python v2 улучшений и без изменений существующего браузерного расширения/DOM-парсинга.

Официальные API провайдеров не используются: мост продолжает работать через локальный брокер и расширение в уже открытых браузерных вкладках ChatGPT, Gemini и Claude.

## Текущее состояние

Рабочее дерево уже было изменено до нативного эксперимента. Существуют важные Python v2 правки, которые надо сохранить:

- `aibridge/job_store.py` и изменения `broker.py`, `client.py`, `command.py` для durable SQLite jobs/results;
- тесты Python v2;
- изменения `extension/src/background.js` и `extension/src/content.js`, включая текущий DOM-парсинг и обработку Gemini;
- соответствующие изменения README про durable journal и Gemini.

Нативный эксперимент добавил:

```text
native/
  CMakeLists.txt
  build.ps1
  README.md
  src/main.cpp
  src/native_bridge.cpp
  src/native_bridge.h
  src/resource.rc
  src/aibridge.manifest
  tests/protocol_smoke.py
  build/... (сгенерированные файлы и aibridge.exe)
scripts/install-native.ps1
scripts/restore-python.ps1
ai-python.cmd
```

Он также заменил `ai.cmd` и добавил в README отдельный раздел `Native Windows prototype`.

Сейчас Windows Scheduled Task `AI Bridge` указывает на `native/build/aibridge.exe --production --hidden`, без триггеров. Пользовательский shim `C:\Users\Ai\.local\bin\ai.cmd` содержит:

```bat
@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
call "C:\workspace\aibridge\ai.cmd" %*
```

То есть после восстановления корневого `ai.cmd` пользовательский shim может остаться неизменным.

## Исходный `ai.cmd` до C++ переключения

```bat
@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
call "%~dp0python_runtime.cmd" -m aibridge.cli %*
```

## Текущий нативный `ai.cmd`

```bat
@echo off
chcp 65001 >nul
setlocal
set "AIBRIDGE_EXE=%~dp0native\build\aibridge.exe"

if /I "%~1"=="debate" (
  call "%~dp0ai-python.cmd" %*
  set "EXIT_CODE=%ERRORLEVEL%"
  endlocal & exit /b %EXIT_CODE%
)

if not exist "%AIBRIDGE_EXE%" (
  echo AI Bridge Native is not built. Run: powershell -ExecutionPolicy Bypass -File "%~dp0native\build.ps1" 1>&2
  endlocal & exit /b 1
)

"%AIBRIDGE_EXE%" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
```

## Существующий Python installer (`scripts/install.ps1`)

Он:

1. Находит Python 3.11+ с установленным `websockets`.
2. Останавливает существующую Scheduled Task `AI Bridge`.
3. Перерегистрирует задачу без триггеров.
4. Action: `pythonw.exe -m aibridge.task_runner`, working directory — корень проекта.
5. Добавляет корень проекта в пользовательский PATH.
6. CLI затем запускает эту task on-demand через lifecycle.

Релевантный фрагмент:

```powershell
$action = $definition.Actions.Create(0)
$action.Path = $runtime.Pythonw
$action.Arguments = "-m aibridge.task_runner"
$action.WorkingDirectory = $root

$registeredTask = $folder.RegisterTaskDefinition(
    $TaskName,
    $definition,
    6,
    $identity,
    $null,
    3,
    $null
)

if ($registeredTask.Definition.Triggers.Count -ne 0) {
    throw "Task '$TaskName' was registered with an unexpected trigger."
}
```

## Требуемый результат

Предложите безопасный минимальный rollback:

- вернуть корневой `ai.cmd` на прямой запуск `python_runtime.cmd -m aibridge.cli`;
- перерегистрировать Scheduled Task через существующий `scripts/install.ps1`;
- удалить `native/`, включая build artifacts и `.exe`;
- удалить только нативные вспомогательные файлы `scripts/install-native.ps1`, `scripts/restore-python.ps1`, `ai-python.cmd`;
- удалить только раздел README про native prototype, сохранив Python v2 документацию;
- не менять `extension/` и DOM-парсинг;
- не откатывать существующие Python v2 изменения;
- проверить, что команда `ai --version` или эквивалентный безопасный CLI smoke test работает без запуска браузерной автоматизации;
- проверить Scheduled Task: action указывает на Python, триггеров 0;
- убедиться, что процессов `aibridge.exe` и нативных listeners не осталось перед удалением build artifacts.

## Наблюдения и гипотезы

Моя гипотеза: отдельный rollback-скрипт после выполнения больше не нужен, потому что пользователь требует полностью убрать C++ ветку. Это предположение, не доказанный факт; оцените, безопаснее ли удалить его или временно оставить.

Моя гипотеза: пользовательский shim в `.local\bin` менять не нужно, поскольку он уже вызывает корневой `ai.cmd`. Проверьте эту гипотезу.

## Вопросы

1. Какой точный порядок операций минимизирует риск оставить запущенный EXE, занятой порт или Scheduled Task со ссылкой на удалённый файл?
2. Какие именно файлы следует удалить, а какие сохранить?
3. Нужно ли менять пользовательский shim или PATH?
4. Какие быстрые проверки достаточны после rollback без отправки реального промта во вкладки?
5. Какие edge cases учесть: запущенный native process, task in Running state, Python runtime selection, грязное рабочее дерево, locked `.exe`?
6. Предложите конкретный план и, если нужны изменения кода/скриптов, минимальный diff.

Самостоятельно определите лучший подход по контексту. Не ограничивайтесь оценкой гипотез локального агента. Если данных недостаточно, не гадайте: явно укажите, чего не хватает.

## Общие требования к качеству кода

Пишите production-ready код: безопасный, быстрый, читаемый и поддерживаемый. Выбирайте простое решение без оверинжиниринга. Учитывайте edge cases, валидацию, ошибки и производительность. Соблюдайте SOLID, DRY и KISS там, где это уместно. Добавляйте тесты или примеры использования только тогда, когда это действительно нужно для задачи.

Не продвигайте гипотезы локального агента как заведомо правильные. Проверьте их и предложите более безопасный, производительный или современный вариант, если он отличается. Укажите риски и edge cases и дайте конкретный план изменений.
