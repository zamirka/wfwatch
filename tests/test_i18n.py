"""The message catalog, plural forms and the choice of the interface language."""

import ast
import re
from pathlib import Path

import pytest

import wfwatch
from wfwatch import cli, i18n, theme
from wfwatch.widgets import SESSION_COUNTS

SRC = Path(wfwatch.__file__).parent


def forms(msg) -> tuple[str, ...]:
    return msg if isinstance(msg, tuple) else (msg,)


# ---- the catalog ----


@pytest.mark.parametrize("key", sorted(i18n.MESSAGES))
def test_catalog_entry_is_complete(key):
    entry = i18n.MESSAGES[key]
    assert set(entry) == set(i18n.LANGS)
    en, ru = entry["en"], entry["ru"]
    assert isinstance(en, tuple) == isinstance(ru, tuple), "a plural in one language is a plural in all"
    if isinstance(en, tuple):
        assert (len(en), len(ru)) == (2, 3)  # en: one, other; ru: one, few, many
        assert all("{n}" in f for f in en + ru)
    names = {frozenset(i18n.placeholders(f)) for f in forms(en) + forms(ru)}
    assert len(names) == 1, f"placeholders differ: {names}"
    for f in forms(en) + forms(ru):
        assert f.format(**dict.fromkeys(next(iter(names)), "x"))  # no stray braces
        assert "—" not in f  # the en dash is used throughout
    assert not re.search(r"[А-Яа-яЁё]", "".join(forms(en))), "Cyrillic in the English text"


def _literals() -> set[str]:
    out = set()
    for p in SRC.glob("*.py"):
        if p.name == "i18n.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
    return out


def test_every_key_in_code_exists_and_every_key_is_used():
    literals = _literals()
    prefixes = {k.split(".")[0] for k in i18n.MESSAGES}
    looks_like_key = {s for s in literals if re.fullmatch(r"[a-z]+\.[a-z_]+", s) and s.split(".")[0] in prefixes}
    assert looks_like_key - set(i18n.MESSAGES) == set(), "keys used in the code but missing from the catalog"
    # built at run time: status_count(status, n) and plural(f"count.{state}", n) in the overview header
    dynamic = {f"count.{s}" for s in [*theme.COUNTED, theme.SUPERSEDED, *SESSION_COUNTS]}
    assert dynamic - set(i18n.MESSAGES) == set()
    assert set(i18n.MESSAGES) - literals - dynamic == set(), "unused keys"


@pytest.mark.parametrize(
    ("n", "form"),
    [
        (0, 2),
        (1, 0),
        (2, 1),
        (3, 1),
        (4, 1),
        (5, 2),
        (10, 2),
        (11, 2),
        (12, 2),
        (14, 2),
        (15, 2),
        (20, 2),
        (21, 0),
        (22, 1),
        (24, 1),
        (25, 2),
        (101, 0),
        (111, 2),
        (112, 2),
        (122, 1),
        (1000, 2),
        (-1, 0),
    ],
)
def test_russian_plural_forms(n, form):
    assert i18n.plural_index("ru", n) == form


def test_english_plural_forms():
    assert [i18n.plural_index("en", n) for n in (0, 1, 2, 11, 21)] == [1, 0, 1, 1, 1]


def test_phrases_and_plurals():
    assert i18n.plural("count.subagents", 1) == "1 subagent" and i18n.plural("count.subagents", 3) == "3 subagents"
    assert i18n.t("more.plus", n=4) == "+4 more"
    i18n.use_lang("ru")
    got = [i18n.plural("count.subagents", n) for n in (1, 2, 5, 11, 21)]
    assert got == ["1 субагент", "2 субагента", "5 субагентов", "11 субагентов", "21 субагент"]
    assert i18n.plural("more.agents", 3, key=2) == "… ещё 3 агента – клавиша 2"
    assert i18n.plural("count.running", 1) == "1 идёт" and i18n.plural("count.running", 2) == "2 идут"
    assert i18n.t("no.such.key") == "no.such.key"  # never fails at run time


def test_styled_keeps_the_style_of_a_placeholder():
    assert i18n.styled("node.session", "dim", session=("demo", "bold")) == [("demo", "bold"), (" · session", "dim")]
    i18n.use_lang("ru")
    assert i18n.styled("more.up", "s", n=3, key=("PgUp", "k")) == [
        ("↑ ещё ", "s"),
        ("3", "s"),
        (" – ", "s"),
        ("PgUp", "k"),
    ]


# ---- choosing the language ----


@pytest.mark.parametrize(
    ("env", "lang"),
    [
        ({}, "en"),
        ({"LANG": "ru_RU.UTF-8"}, "ru"),
        ({"LANG": "en_US.UTF-8"}, "en"),
        ({"LANG": "C.UTF-8"}, "en"),
        ({"LANG": "ru_RU.UTF-8", "LC_MESSAGES": "en_US.UTF-8"}, "en"),  # LC_MESSAGES before LANG
        ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "ru_RU.UTF-8"}, "ru"),
        ({"LC_MESSAGES": "ru_RU", "LC_ALL": "C"}, "en"),  # LC_ALL before everything
        ({"LC_ALL": "ru_UA.UTF-8"}, "ru"),
        ({"LANG": "", "LC_ALL": "", "LC_MESSAGES": "ru"}, "ru"),  # empty means not set
        ({"WFWATCH_LANG": "ru", "LANG": "en_US.UTF-8"}, "ru"),  # WFWATCH_LANG before the locale
        ({"WFWATCH_LANG": "en", "LC_ALL": "ru_RU.UTF-8"}, "en"),
        ({"WFWATCH_LANG": "RU"}, "ru"),
        ({"WFWATCH_LANG": "de", "LANG": "ru_RU.UTF-8"}, "ru"),  # an unknown language: as if not set
        ({"WFWATCH_LANG": "auto", "LANG": "ru_RU.UTF-8"}, "ru"),
        ({"LANG": "russian"}, "en"),
    ],
)
def test_language_detection(env, lang):
    assert i18n.detect_lang(env) == lang


def test_use_lang():
    assert i18n.use_lang("ru") == "ru" and i18n.current() == "ru"
    assert i18n.use_lang("auto", {"LANG": "ru_RU.UTF-8"}) == "ru"
    assert i18n.use_lang("auto", {}) == "en"
    assert i18n.use_lang("xx", {}) == "en"


# ---- the command line ----


def test_lang_option_env_and_restore(demo_run, capsys, monkeypatch):
    assert cli.main(["--once", "--overview", "--width", "120"]) == 0
    assert "WFWATCH  overview" in capsys.readouterr().out
    assert cli.main(["--once", "--overview", "--width", "120", "--lang", "ru"]) == 0
    assert "WFWATCH  обзор" in capsys.readouterr().out
    assert i18n.current() == "en"  # restored
    monkeypatch.setenv("WFWATCH_LANG", "ru")
    assert cli.main(["--once", "--overview", "--width", "120"]) == 0
    assert "обзор" in capsys.readouterr().out
    assert cli.main(["--once", "--overview", "--width", "120", "--lang=en"]) == 0  # the option beats the variable
    assert "overview" in capsys.readouterr().out
    monkeypatch.delenv("WFWATCH_LANG")
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    assert cli.main(["--list", "--width", "120"]) == 0
    assert "состояние" in capsys.readouterr().out
    assert cli.main(["no-such-run", "--lang", "ru"]) == 1
    assert "Запуск «no-such-run» не найден" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("lang", "expect"),
    [
        ("en", ["usage: wfwatch", "arguments:", "options:", "show this help and exit", "interface language"]),
        ("ru", ["использование: wfwatch", "аргументы:", "параметры:", "показать эту справку", "язык интерфейса"]),
    ],
)
def test_help_is_in_the_interface_language(lang, expect, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help", "--lang", lang])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for s in expect:
        assert s in out, out
    for s in ("--list", "--once", "--theme", "--lang", "--debug", "{auto,en,ru}"):
        assert s in out
    if lang == "en":
        assert not re.search(r"[А-Яа-яЁё]", out), out


def test_bad_lang_value_is_rejected(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--lang", "de", "--once"])
    assert e.value.code == 2
    assert "--lang" in capsys.readouterr().err
    assert i18n.current() == "en"


def test_lang_option_is_found_before_parsing():
    assert cli.lang_option(["--once", "--lang", "ru"]) == "ru"
    assert cli.lang_option(["--lang=ru", "-o"]) == "ru"
    assert cli.lang_option(["--lang"]) == "auto"
    assert cli.lang_option(["--", "--lang", "ru"]) == "auto"
