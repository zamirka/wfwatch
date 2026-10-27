"""Interface language: the message catalog, plural forms and the choice of language.

English is the default; Russian is chosen with --lang ru, WFWATCH_LANG=ru or a ru_* locale.
Every string the interface shows goes through ``t`` (a phrase) or ``plural`` (a phrase with a
number); whole phrases with named placeholders are stored, never pieces glued together.

A plural entry holds the forms of the language: English has two (one, other), Russian three
(one: 1, 21, 101; few: 2–4, 22–24; many: 0, 5–20, 25–30)."""

from __future__ import annotations

import os
import re
import string

LANGS = ("en", "ru")
CHOICES = ("auto", *LANGS)
DEFAULT = "en"

Message = str | tuple[str, ...]  # a phrase, or its plural forms

MESSAGES: dict[str, dict[str, Message]] = {
    # ---- states of a workflow run ----
    "run.running": {"en": "running", "ru": "идёт"},
    "run.completed": {"en": "completed", "ru": "завершён"},
    "run.failed": {"en": "failed", "ru": "ошибка"},
    "run.killed": {"en": "killed", "ru": "остановлен"},
    "run.stopped": {"en": "session closed", "ru": "сессия закрыта"},
    "run.unknown": {"en": "no data", "ru": "нет данных"},
    # ---- states of a session ----
    "session.busy": {"en": "working", "ru": "работает"},
    "session.waiting": {"en": "waiting for you", "ru": "ждёт вас"},
    "session.background": {"en": "background task", "ru": "фоновая задача"},
    "session.idle": {"en": "idle", "ru": "свободна"},
    "where.cli": {"en": "terminal", "ru": "терминал"},
    # ---- statuses of agents and phases ----
    "status.running": {"en": "running", "ru": "идёт"},
    "status.done": {"en": "done", "ru": "готово"},
    "status.failed": {"en": "failed", "ru": "ошибка"},
    "status.stale": {"en": "interrupted", "ru": "прервано"},
    "status.pending": {"en": "pending", "ru": "ожидает"},
    "status.superseded": {"en": "replaced", "ru": "заменён"},
    "status.skipped": {"en": "never ran", "ru": "не запускался"},
    "agent.silent": {"en": "silent", "ru": "тишина"},
    "phase.none": {"en": "no phase", "ru": "без этапа"},
    # ---- counts ----
    "count.running": {"en": ("{n} running", "{n} running"), "ru": ("{n} идёт", "{n} идут", "{n} идут")},
    "count.done": {"en": ("{n} done", "{n} done"), "ru": ("{n} готов", "{n} готовы", "{n} готово")},
    "count.failed": {"en": ("{n} failed", "{n} failed"), "ru": ("{n} с ошибкой", "{n} с ошибкой", "{n} с ошибкой")},
    "count.stale": {
        "en": ("{n} interrupted", "{n} interrupted"),
        "ru": ("{n} прерван", "{n} прерваны", "{n} прервано"),
    },
    "count.superseded": {"en": ("{n} replaced", "{n} replaced"), "ru": ("{n} заменён", "{n} заменены", "{n} заменено")},
    "count.finished": {
        "en": ("{n} finished", "{n} finished"),
        "ru": ("{n} завершён", "{n} завершены", "{n} завершено"),
    },
    "count.busy": {"en": ("{n} working", "{n} working"), "ru": ("{n} работает", "{n} работают", "{n} работают")},
    "count.waiting": {
        "en": ("{n} waiting for you", "{n} waiting for you"),
        "ru": ("{n} ждёт вас", "{n} ждут вас", "{n} ждут вас"),
    },
    "count.background": {
        "en": ("{n} in background", "{n} in background"),
        "ru": ("{n} в фоне", "{n} в фоне", "{n} в фоне"),
    },
    "count.idle": {"en": ("{n} idle", "{n} idle"), "ru": ("{n} свободна", "{n} свободны", "{n} свободны")},
    "count.subagents": {
        "en": ("{n} subagent", "{n} subagents"),
        "ru": ("{n} субагент", "{n} субагента", "{n} субагентов"),
    },
    "count.workflows": {
        "en": ("{n} workflow", "{n} workflows"),
        "ru": ("{n} workflow", "{n} workflow", "{n} workflow"),
    },
    "count.sessions": {"en": ("{n} session", "{n} sessions"), "ru": ("{n} сессия", "{n} сессии", "{n} сессий")},
    "count.calls": {"en": ("{n} call", "{n} calls"), "ru": ("{n} вызов", "{n} вызова", "{n} вызовов")},
    "progress.done": {"en": "{done}/{total} done", "ru": "{done}/{total} готово"},
    "progress.running": {
        "en": ("{n} running / {total}", "{n} running / {total}"),
        "ru": ("{n} идёт / {total}", "{n} идут / {total}", "{n} идут / {total}"),
    },
    # ---- what did not fit ----
    "more.plus": {"en": "+{n} more", "ru": "+{n} ещё"},
    "more.dots": {"en": "… {n} more", "ru": "… ещё {n}"},
    "more.up": {"en": "↑ {n} more – {key}", "ru": "↑ ещё {n} – {key}"},
    "more.down": {"en": "↓ {n} more – {key}", "ru": "↓ ещё {n} – {key}"},
    "more.key": {"en": "… {n} more – key {key}", "ru": "… ещё {n} – клавиша {key}"},
    "more.agents": {
        "en": ("… {n} more agent – key {key}", "… {n} more agents – key {key}"),
        "ru": (
            "… ещё {n} агент – клавиша {key}",
            "… ещё {n} агента – клавиша {key}",
            "… ещё {n} агентов – клавиша {key}",
        ),
    },
    "more.items": {"en": "… {what} not shown – {key}", "ru": "… ещё {what} – {key}"},
    "more.and": {"en": "{a} and {b}", "ru": "{a} и {b}"},
    "time.ago": {"en": "{dur} ago", "ru": "{dur} назад"},
    # ---- overview ----
    "overview.title": {"en": "overview", "ru": "обзор"},
    "overview.runs": {"en": "workflows: {running}", "ru": "workflow: {running}"},
    "overview.sessions": {"en": "sessions: ", "ru": "сессии: "},
    "overview.note": {
        "en": "all projects · running and recent workflows (last {keep} min) and sessions (last {idle} min)",
        "ru": "все проекты · идущие и недавние workflow (до {keep} мин) и сессии (до {idle} мин)",
    },
    "overview.empty": {
        "en": "Nothing is running right now. New workflows and sessions will show up here on their own.",
        "ru": "Сейчас ничего не запущено. Новые workflow и сессии появятся здесь сами.",
    },
    "overview.owns": {"en": "runs workflow {nums}", "ru": "ведёт workflow {nums}"},
    "overview.no_workflow": {"en": "no workflow", "ru": "сессия без workflow"},
    "overview.agents": {"en": "agents in all runs: ", "ru": "агенты во всех запусках: "},
    "overview.bg": {"en": "background tasks: {n}", "ru": "фоновых задач: {n}"},
    # ---- log ----
    "log.title": {"en": "log", "ru": "журнал"},
    "log.workflow": {"en": "workflow", "ru": "workflow"},
    "log.started": {"en": "started", "ru": "запущен"},
    "log.done": {"en": "done", "ru": "готово"},
    "log.failed": {"en": "failed", "ru": "ошибка"},
    "log.launched": {"en": "workflow launched", "ru": "workflow запущен"},
    "log.relaunched": {"en": "workflow resumed", "ru": "workflow перезапущен"},
    "log.finished": {"en": "workflow {state}", "ru": "workflow: {state}"},
    # ---- what an agent does (tool names are shown as they are; these few are phrases) ----
    "verb.search": {"en": "Search", "ru": "Поиск"},
    "verb.fetch": {"en": "Fetch", "ru": "Загрузка"},
    "verb.result": {"en": "returning the result", "ru": "отдаёт результат"},
    # ---- key bar ----
    "key.open": {"en": "open", "ru": "открыть"},
    "key.goto": {"en": "go to", "ru": "перейти"},
    "key.next": {"en": "next", "ru": "листать"},
    "key.overview": {"en": "overview", "ru": "обзор"},
    "key.phase": {"en": "phase", "ru": "этап"},
    "key.scroll": {"en": "scroll", "ru": "листать"},
    "key.follow": {"en": "follow", "ru": "следить"},
    "key.diagram": {"en": "diagram", "ru": "схема"},
    "key.cards": {"en": "cards", "ru": "карточки"},
    "key.collapse": {"en": "collapse", "ru": "свернуть"},
    "key.expand": {"en": "expand", "ru": "развернуть"},
    "key.quit": {"en": "quit", "ru": "выход"},
    # ---- cards ----
    "card.stats": {"en": "{calls} · ctx {ctx} · out {out}", "ru": "{calls} · ctx {ctx} · out {out}"},
    "card.stats_short": {"en": "{calls} · ctx {ctx}", "ru": "{calls} · ctx {ctx}"},
    "cards.no_agents": {"en": "no agents have started yet…", "ru": "агенты ещё не запускались…"},
    # ---- status lines ----
    "footer.agent": {"en": "agent", "ru": "агент"},
    "footer.agents": {"en": "agents", "ru": "агенты"},
    "footer.models": {"en": ("{n} model", "{n} models"), "ru": ("{n} модель", "{n} модели", "{n} моделей")},
    "footer.failed": {"en": "failed", "ru": "ошибки"},
    "footer.stale": {"en": "interrupted", "ru": "прервано"},
    "footer.phase": {"en": "phase", "ru": "этап"},
    "footer.calls": {"en": "calls", "ru": "вызовов"},
    "footer.out": {"en": "out", "ru": "out"},
    "footer.session": {"en": "session", "ru": "сессия"},
    "footer.subagents": {"en": "subagents", "ru": "субагенты"},
    "footer.bg": {"en": "bg", "ru": "фон"},
    "footer.ctx": {"en": "ctx", "ru": "ctx"},
    "footer.last_action": {"en": "last action", "ru": "последнее действие"},
    # ---- the diagram ----
    "fan.phase": {"en": "phase agents", "ru": "агенты этапа"},
    "fan.phase_empty": {"en": "no agents in this phase yet", "ru": "агенты этапа ещё не запускались"},
    "fan.phase_more": {"en": "more – key {key}", "ru": "больше – клавиша {key}"},
    "fan.session": {"en": "session subagents", "ru": "субагенты сессии"},
    "fan.session_empty": {"en": "no subagents", "ru": "субагентов нет"},
    "fan.session_more": {"en": "all – key {key}", "ru": "все – клавиша {key}"},
    "diagram.no_fit": {"en": "does not fit – key {key}: cards", "ru": "не помещается – клавиша {key}: карточки"},
    "diagram.phases": {"en": "phases", "ru": "этапы"},
    "diagram.launch": {"en": "launch", "ru": "запуск"},
    "diagram.finish": {"en": "finish", "ru": "финиш"},
    "diagram.session": {"en": "session", "ru": "сессия"},
    "node.session": {"en": "{session} · session", "ru": "{session} · сессия"},
    "node.launched": {"en": "launched the workflow", "ru": "запустила workflow"},
    "node.result_pending": {"en": "the result goes back to the session", "ru": "итог вернётся в сессию"},
    "node.result_done": {"en": "the result is in the session", "ru": "итог в сессии"},
    "stats.agents": {"en": "agents", "ru": "агенты"},
    "stats.calls": {"en": "calls", "ru": "вызовов"},
    "stats.out": {"en": "out", "ru": "out"},
    "stats.time": {"en": "time", "ru": "время"},
    # ---- a session ----
    "session.subagents": {"en": "session subagents · {progress}", "ru": "субагенты сессии · {progress}"},
    "session.no_subagents": {"en": "no subagents", "ru": "субагентов нет"},
    "session.alone": {
        "en": "no subagents – the session works on its own",
        "ru": "субагентов нет – сессия работает сама",
    },
    "session.back": {"en": "↩ subagent results go back to the session", "ru": "↩ итоги субагентов – обратно в сессию"},
    "session.actions": {"en": "latest actions", "ru": "последние действия"},
    "session.newest": {"en": "newest first", "ru": "новые сверху"},
    "session.nothing": {"en": "nothing yet", "ru": "пока ничего не видно"},
    # ---- background commands ----
    "bg.title": {"en": "background tasks", "ru": "фоновые задачи"},
    "bg.label": {"en": "background: ", "ru": "фон: "},
    "bg.now": {"en": "now {proc} {cpu}%", "ru": "сейчас {proc} {cpu}%"},
    "bg.lost": {"en": "lost: the session restarted", "ru": "прервана: сессия перезапущена"},
    "bg.exit": {"en": "exit code {code}", "ru": "код выхода {code}"},
    "bg.killed": {"en": "killed", "ru": "остановлена"},
    "bg.failed": {"en": "failed", "ru": "ошибка"},
    "bg.done": {"en": "done", "ru": "готово"},
    # ---- errors ----
    "error.render": {
        "en": "⚠ render error: {msg} – o: overview, q: quit, details: wfwatch --debug",
        "ru": "⚠ ошибка отрисовки: {msg} – o: обзор, q: выход, подробности: wfwatch --debug",
    },
    "error.read": {
        "en": "⚠ read error – {msg}{more}; details: wfwatch --debug",
        "ru": "⚠ ошибка чтения – {msg}{more}; подробности: wfwatch --debug",
    },
    # ---- --list ----
    "list.run": {"en": "run", "ru": "запуск"},
    "list.workflow": {"en": "workflow", "ru": "workflow"},
    "list.project": {"en": "project", "ru": "проект"},
    "list.session": {"en": "session", "ru": "сессия"},
    "list.state": {"en": "state", "ru": "состояние"},
    "list.agents": {"en": "agents", "ru": "агенты"},
    "list.time": {"en": "time", "ru": "время"},
    # ---- command line ----
    "cli.description": {
        "en": "Live view of Claude Code workflows and sessions: phases, agents, log.",
        "ru": "Живая схема workflow и сессий Claude Code: этапы, агенты, журнал.",
    },
    "cli.usage": {"en": "usage: ", "ru": "использование: "},
    "cli.arguments": {"en": "arguments", "ru": "аргументы"},
    "cli.options": {"en": "options", "ru": "параметры"},
    "cli.not_found": {
        "en": "Run “{run}” not found. Recent runs: wfwatch --list",
        "ru": "Запуск «{run}» не найден. Список: wfwatch --list",
    },
    "cli.windows": {
        "en": "the full-screen mode does not work on Windows – run wfwatch in WSL (or use --list and --once)",
        "ru": "полноэкранный режим в Windows не работает – запустите wfwatch в WSL (или используйте --list и --once)",
    },
    "cli.no_tty": {
        "en": "the full-screen mode needs a terminal; to print one frame to a file or a pipe, use --once",
        "ru": "полноэкранному режиму нужен терминал; чтобы вывести один кадр в файл или pipe, используйте --once",
    },
    "help.help": {"en": "show this help and exit", "ru": "показать эту справку и выйти"},
    "help.version": {"en": "show the version and exit", "ru": "показать версию и выйти"},
    "help.run": {
        "en": "a run id (wf_…), its beginning or a part of the workflow name; without it – everything that runs",
        "ru": "id запуска (wf_…), его начало или часть названия; без него – все идущие",
    },
    "help.list": {"en": "list recent runs", "ru": "список последних запусков"},
    "help.n": {"en": "how many runs --list shows", "ru": "сколько запусков показать в --list"},
    "help.overview": {
        "en": "start with the overview, even if only one workflow runs",
        "ru": "начать с обзора, даже если идёт один запуск",
    },
    "help.all": {
        "en": "cards view: expand every phase, not only the active ones",
        "ru": "в карточках: развернуть все этапы, а не только активные",
    },
    "help.cards": {
        "en": "show the detailed view as cards, not as a diagram",
        "ru": "подробный вид карточками, а не схемой",
    },
    "help.once": {"en": "print one frame and exit", "ru": "напечатать один кадр и выйти"},
    "help.width": {"en": "output width for --once and --list", "ru": "ширина вывода для --once и --list"},
    "help.theme": {
        "en": "colors: dark – for a dark background, light – for a light one, auto – from COLORFGBG (dark if unknown)",
        "ru": "цвета: dark – для тёмного фона, light – для светлого, auto – по COLORFGBG (иначе dark)",
    },
    "help.lang": {
        "en": "interface language: en, ru, or auto – from WFWATCH_LANG, then the locale (LC_ALL, LC_MESSAGES, LANG)",
        "ru": "язык интерфейса: en, ru или auto – по WFWATCH_LANG, затем по локали (LC_ALL, LC_MESSAGES, LANG)",
    },
    "help.debug": {
        "en": "do not catch read and render errors (for a bug report)",
        "ru": "не перехватывать ошибки чтения и отрисовки (для отчёта об ошибке)",
    },
}

_lang = DEFAULT


# ---- choosing the language ----


def _norm(value: str | None) -> str | None:
    """A language from a value like "ru", "ru_RU.UTF-8" or "en-US"; None for anything else."""
    v = (value or "").strip().lower()
    for lang in LANGS:
        if v == lang or v.startswith((lang + "_", lang + "-", lang + ".")):
            return lang
    return None


def detect_lang(environ=None) -> str:
    """WFWATCH_LANG if it names a known language; else the locale: the first set of LC_ALL,
    LC_MESSAGES, LANG decides (ru* – Russian, anything else – English); else English."""
    env = os.environ if environ is None else environ
    lang = _norm(env.get("WFWATCH_LANG"))
    if lang:
        return lang
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if env.get(var):
            return "ru" if _norm(env[var]) == "ru" else DEFAULT
    return DEFAULT


def use_lang(name: str | None = "auto", environ=None) -> str:
    """Switch the language (auto, en or ru); returns the one chosen."""
    global _lang
    _lang = name if name in LANGS else detect_lang(environ)
    return _lang


def current() -> str:
    return _lang


# ---- messages ----


def plural_index(lang: str, n: int) -> int:
    """Which plural form a number takes."""
    n = abs(int(n))
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return 0
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return 1
        return 2
    return 0 if n == 1 else 1


def template(key: str, /, n: int | None = None) -> str:
    """The raw phrase of the current language (falling back to English, then to the key itself)."""
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    msg = entry.get(_lang, entry.get(DEFAULT, key))
    if isinstance(msg, tuple):
        lang = _lang if _lang in entry else DEFAULT
        return msg[min(plural_index(lang, n or 0), len(msg) - 1)]
    return msg


def t(key: str, /, **kw) -> str:
    """A phrase with its placeholders filled in."""
    s = template(key, kw.get("n"))
    return s.format(**kw) if kw else s


def plural(key: str, n: int, /, **kw) -> str:
    """A phrase with a number: the form that agrees with ``n``."""
    return template(key, n).format(n=n, **kw)


_FIELD = re.compile(r"\{(\w+)\}")


def styled(key: str, /, style: str = "", n: int | None = None, **kw) -> list[tuple[str, str]]:
    """A phrase as (text, style) parts, so a placeholder can have a style of its own: a value given
    as a (text, style) pair keeps its style, the rest of the phrase gets ``style``."""
    s = template(key, n)
    if n is not None:
        kw.setdefault("n", n)
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _FIELD.finditer(s):
        if m.start() > pos:
            out.append((s[pos : m.start()], style))
        v = kw.get(m[1], "")
        out.append(v if isinstance(v, tuple) else (str(v), style))
        pos = m.end()
    if pos < len(s):
        out.append((s[pos:], style))
    return [p for p in out if p[0]]


def placeholders(msg: Message) -> set[str]:
    """Names of the placeholders of a phrase (of all its forms)."""
    forms = msg if isinstance(msg, tuple) else (msg,)
    return {name for f in forms for _, name, _, _ in string.Formatter().parse(f) if name}
