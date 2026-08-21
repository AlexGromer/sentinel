#!/usr/bin/env python3
"""Карта деградаций покрывает КАЖДЫЙ компонент развёртывания — W5.

Run:  .venv/bin/python tests/test_degradation_map_offline.py

ЗАЧЕМ ГЕЙТ У ДОКУМЕНТА. `docs/DEGRADATION_MAP.md` — рукописный перечень, а такие в этом репозитории
гниют по одному и тому же сценарию: лишнее в них видно (строка про удалённое роняет прогон), а
ПРОПУЩЕННОЕ — нет, потому что отсутствие не имеет представления. Ровно так таблица сервисов в
`docs/DISTRIBUTION.md` прожила без строки `orchestrator` со дня появления этого сервиса.

Поэтому состав компонентов не записан здесь, а ВЫВОДИТСЯ из трёх источников, которые меняются
вместе с продуктом:
  1. пробы готовности   — `cmd/control-api/readyz.go`
  2. сервисы поставки   — `services:` трёх compose-файлов
  3. собственные бинари — каталоги `cmd/*`
Новый компонент попадает в проверку по построению, а не потому что кто-то вспомнил.

⚠ ЧЕСТНАЯ ГРАНИЦА ЭТОЙ ПРОВЕРКИ. Она требует, чтобы имя компонента ВСТРЕЧАЛОСЬ в карте, и не умеет
судить, осмысленна ли строка про него: имя, упомянутое в проходной фразе, её удовлетворит. Это
сознательный размен — проверять содержательность абзаца текстом невозможно, а проверять
присутствие можно, и именно присутствия не хватало таблице сервисов. Содержательность держит
человек, который карту читает; пол на число строк держит то, что таблица не усохла.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_RU = os.path.join(REPO, "docs", "DEGRADATION_MAP.md")
MAP_EN = os.path.join(REPO, "docs", "DEGRADATION_MAP.en.md")
COMPOSE = ("docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.offline.yml")

# Чуть НИЖЕ замеренного на 2026-08-21 (12 строк в основной таблице). Пол может только расти.
ROW_FLOOR = 10

failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def derived_components() -> "set[str]":
    """Состав развёртывания, выведенный из кода и compose — источник строк карты."""
    names: "set[str]" = set()
    readyz = read(os.path.join(REPO, "cmd", "control-api", "readyz.go"))
    names |= set(re.findall(r'checks\["([a-z][a-z0-9_-]*)"\]', readyz))
    for f in COMPOSE:
        body = read(os.path.join(REPO, f)).split("\nservices:", 1)[-1]
        names |= set(re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", body, re.M))
    cmd_dir = os.path.join(REPO, "cmd")
    names |= {d for d in os.listdir(cmd_dir) if os.path.isdir(os.path.join(cmd_dir, d))}
    return names


def test_the_derivation_still_finds_things() -> None:
    """Пустой обход покрывает пустое множество идеально — только счёт отличает это от работы."""
    n = len(derived_components())
    if n < 12:
        fail(f"обход нашёл {n} компонент(ов) — источники перестали читаться, и это выглядит "
             f"как «всё покрыто»")


def test_every_component_appears_in_the_map() -> None:
    """Компонент без строки — это компонент, о смерти которого никто не предупредит."""
    text = read(MAP_RU)
    missing = sorted(c for c in derived_components() if not re.search(rf"\b{re.escape(c)}\b", text))
    if missing:
        fail(f"{len(missing)} компонент(ов) выведены из readyz/compose/cmd и НЕ упомянуты в "
             f"docs/DEGRADATION_MAP.md: {', '.join(missing)} — либо их деградация не описана, либо "
             f"они перестали существовать и карта отстала")


def test_the_main_table_did_not_shrink() -> None:
    """Пол на число строк: карта, потерявшая строки, читается как карта без деградаций."""
    rows = [l for l in read(MAP_RU).splitlines()
            if l.startswith("| **") or (l.startswith("| ") and "|" in l[2:] and "---" not in l)]
    body = [l for l in rows if not l.startswith("| Компонент") and "---" not in l]
    if len(body) < ROW_FLOOR:
        fail(f"в карте {len(body)} строк(и) при поле {ROW_FLOOR}")


def test_both_language_halves_exist_and_cover_the_same_components() -> None:
    """Двуязычность гейтится; карта — не исключение."""
    if not os.path.exists(MAP_EN):
        fail("docs/DEGRADATION_MAP.en.md не существует — двуязычность обязательна")
        return
    en = read(MAP_EN)
    missing = sorted(c for c in derived_components() if not re.search(rf"\b{re.escape(c)}\b", en))
    if missing:
        fail(f"английская половина карты не упоминает: {', '.join(missing)}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print(f"degradation map: OK ({len(derived_components())} компонентов выведено из "
          f"readyz/compose/cmd, каждый назван в обеих половинах карты, пол {ROW_FLOOR} держится)")
