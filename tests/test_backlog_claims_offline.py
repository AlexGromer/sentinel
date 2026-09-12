#!/usr/bin/env python3
"""[BACKLOG-RUN-PARAMS-ARITHMETIC-STALE] — выводимая величина, записанная в реестр литералом, сверяется
с замером.

ЧТО БЫЛО ЗАМЕРЕНО, И ПОЧЕМУ ОШИБЛИСЬ ОБЕ ЗАПИСИ. Задача `[RUN-PARAMS-LOCKED-IN-THE-ADMIN-SECTION]`
измеряла свой объём долей «9 из 16 настроек», и знаменатель протух через два дня: ADR-165 вырастил
`settings` с 16 до 44. Поправлявшая её запись назвала новую долю «9 из 44» — и ЭТО ТОЖЕ НЕВЕРНО:
числитель никто не переизмерил. Замер 2026-09-12: пер-прогонное поле есть РОВНО У ОДНОЙ настройки из
44 (`heal_llm`), то есть на один прогон нельзя задать 43 из 44. Существо претензии верно и только
усиливается — протухала арифметика, которой она себя мерила.

ПОЧЕМУ ГЕЙТ ИМЕННО ТАКОЙ, А НЕ «НИ ОДНО ЧИСЛО В РЕЕСТРЕ НЕ ПРОТУХЛО». Общая форма была написана
первой и отвергнута ЗАМЕРОМ: реестр законно цитирует ИСТОРИЧЕСКИЕ числа («было 16, стало 44»), и
правило «никакое число не отличается от измеренного» краснеет на собственном исправленном тексте этой
самой задачи. Проверка прозы на намерение — не проверка, а угадывание. Поэтому здесь сверяется
ИМЕНОВАННОЕ утверждение: запись, которая называет состав `settingsSchema`, обязана называть
ИЗМЕРЕННЫЕ числа. Обе стороны выводятся — числа из Go, текст из реестра, — и ни одна не переписана
в тест.

Run: .venv/bin/python tests/test_backlog_claims_offline.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIMANT = "[RUN-PARAMS-LOCKED-IN-THE-ADMIN-SECTION]"

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:500])


def measure():
    """Состав схемы — из Go, обходом литералов. Три величины, каждая независимо."""
    s = io.open(os.path.join(ROOT, "cmd", "control-api", "main.go"), encoding="utf-8").read()
    i = s.index("var settingsSchema")
    blk = s[i:i + 80000]
    names = re.findall(r'^\t"([a-z_0-9]+)": map\[string\]any\{', blk, re.M)
    entries = re.findall(r'^\t"([a-z_0-9]+)": map\[string\]any\{(.*?)^\t\},', blk, re.M | re.S)
    retention = [n for n, body in entries if re.search(r'"group":\s*"retention"', body)]
    j = s.index('"fields": map[string]any{')
    fields = set(re.findall(r'^\t\t\t"([a-z_0-9]+)":', s[j:j + 12000], re.M))
    per_run = [n for n in names if n in fields]
    return len(names), len(retention), per_run


def test_the_registry_claim_about_the_schema_matches_the_schema():
    total, retention, per_run = measure()
    # Полы: разбор, переставший что-либо находить, «подтвердит» любое число.
    check("состав схемы разобран", total >= 30, "настроек разобрано %d" % total)
    check("группа retention разобрана", retention >= 5, "записей retention %d" % retention)
    check("пересечение с пер-прогонными полями разобрано", len(per_run) >= 1,
          "ни одна настройка не имеет пер-прогонного поля — разбор `fields` отстал")

    # Запись ищется по СВОЕМУ ключу в начале строки, а не по вхождению имени: имя цитируют и соседние
    # записи (именно так эта задача и попала в поле зрения), и обход по вхождению нашёл бы две.
    lines = [l for l in io.open(os.path.join(ROOT, "BACKLOG.md"), encoding="utf-8")
             if l.startswith("- [ ] " + CLAIMANT)]
    check("утверждающая запись найдена в Active", len(lines) == 1,
          "найдено %d записей %s — гейт сверяет именованное утверждение и без него вакуумен"
          % (len(lines), CLAIMANT))
    if len(lines) != 1:
        return
    text = lines[0]

    want = {
        "всего настроек": str(total),
        "записей в retention": str(retention),
        "нельзя задать на прогон": "%d из %d" % (total - len(per_run), total),
    }
    missing = sorted(k for k, v in want.items() if v not in text)
    check("запись называет ИЗМЕРЕННЫЕ числа", not missing,
          "запись измеряет свой объём долей, которой в схеме нет: не названо %s (измерено: всего %d, "
          "retention %d, пер-прогонное поле есть у %s → нельзя задать %d из %d)"
          % (missing, total, retention, per_run, total - len(per_run), total))


def main():
    print("[BACKLOG-RUN-PARAMS-ARITHMETIC-STALE] арифметика реестра против состава схемы")
    test_the_registry_claim_about_the_schema_matches_the_schema()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
