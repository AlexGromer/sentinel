#!/usr/bin/env python3
"""Офлайн-гейт: `docs/OUTPUTS.md` описывает ТУ схему `plan.json`, которую код на самом деле пишет.

Run:  .venv/bin/python tests/test_outputs_schema_offline.py

ЧТО ЭТО ЗАКРЫВАЕТ. §1 объявляет «реальные top-level ключи» и до 2026-08-23 перечислял одиннадцать,
хотя код писал четырнадцать: `perception` появился с ADR-092, `completeness` — с ADR-131,
`degradations` — там же. Ссылка на строки (`brain/graph.py:401-416`) при этом указывала в место, где
этого кода давно нет. Рукописный перечень ключей — то же самое, что рукописный перечень тестов:
он не показывает пропущенное, потому что отсутствие не имеет представления.

⚠ ЦЕНА ИМЕННО ЭТОГО РАСХОЖДЕНИЯ ЗАМЕРЕНА, а не предположена. Интегратор, читающий документ, видит
`coverage_achieved` и не видит `completeness` — и пишет гейт по покрытию. Обход, упёршийся в потолок
шагов, отдаёт `coverage_achieved: 1.0` при тридцати адресах в оставшемся фронтире; гейт зелёный,
сайт пройден на треть. Это тот самый случай, ради которого ADR-131 завёл `completeness`.

ПЕРЕЧЕНЬ ВЫВОДИТСЯ ИЗ КОДА. Ключи собираются разбором дерева `brain/graph.py`: литерал `plan_obj = {…}`
плюс все присваивания `plan_obj["…"] = …` внутри узла `report`. Сверка идёт В ОБЕ СТОРОНЫ: ключ, о
котором документ молчит, и ключ, которого в коде нет, — оба красные. Первое — дрейф документа,
второе — обещание читателю поля, которого он не получит.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
failures: "list[str]" = []


def fail(msg: str) -> None:
    failures.append(msg)


def _plan_keys_from_code() -> "set[str]":
    """Top-level ключи `plan.json`, выведенные из узла `report` (единственного, кто пишет целый план)."""
    tree = ast.parse((REPO / "brain" / "graph.py").read_text())
    report = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "report"), None)
    assert report is not None, "узел `report` исчез — гейт указывает на функцию, которой нет"
    keys: "set[str]" = set()
    for node in ast.walk(report):
        # plan_obj = { "...": ..., ... }
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            if any(isinstance(t, ast.Name) and t.id == "plan_obj" for t in node.targets):
                keys |= {k.value for k in node.value.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # plan_obj["..."] = ...
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == "plan_obj" and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str)):
                    keys.add(t.slice.value)
    return keys


def test_the_documented_schema_is_the_written_schema():
    keys = _plan_keys_from_code()
    if len(keys) < 10:
        fail(f"из кода выведено всего {len(keys)} ключ(ей) ({sorted(keys)}) — разбор сломался, и "
             "гейт прошёл бы по пустому множеству, ничего не проверив")
        return
    for doc in ("docs/OUTPUTS.md", "docs/OUTPUTS.en.md"):
        text = (REPO / doc).read_text()
        # Окно берётся ОТ строки схемы и на 3000 символов вперёд — там же, где интегратор читает
        # перечень. Искать по всему файлу значило бы засчитывать случайное упоминание ключа в
        # соседнем разделе за его описание в схеме.
        # ⚠ ОКНО — САМ ПЕРЕЧЕНЬ, а не абзац вокруг него. Первая редакция брала 3000 символов от
        # строки схемы, и мутация «убрать `completeness` из перечня» ВЫЖИЛА: слово осталось в
        # соседнем абзаце, объясняющем этот ключ, и гейт засчитал не то вхождение. Ровно та ошибка,
        # которую этот репозиторий ловит у себя раз за разом — экзистенциал по слишком широкой
        # области удовлетворяется чем угодно похожим. Границы берутся по фигурным скобкам самого
        # перечня: `{plan_id … }`.
        marker = "`{plan_id"
        if marker not in text:
            fail(f"{doc}: перечня ключей («{{plan_id …») больше нет — гейт указывает в никуда")
            continue
        i = text.index(marker)
        head = text[i:text.index("}`", i) + 2]
        missing = sorted(k for k in keys if k not in head)
        if missing:
            fail(f"{doc}: код пишет ключи, о которых документ молчит: {missing}")
    print(f"  ok  {len(keys)} ключ(ей) выведено из кода и назван(ы) в обеих половинах: {sorted(keys)}")


def test_the_document_does_not_promise_a_key_the_code_never_writes():
    """Встречное утверждение. Без него «назвать в документе всё подряд» проходит проверку выше идеально,
    а читатель получает поле, которого в файле не будет."""
    keys = _plan_keys_from_code()
    # Кандидаты берутся из САМОЙ строки схемы, а не из головы: это перечень, который читает интегратор.
    text = (REPO / "docs" / "OUTPUTS.md").read_text()
    i = text.index("Схема (реальные top-level ключи")
    line = text[i:text.index("`.", i)]
    import re
    named = {m for m in re.findall(r"\b([a-z_]{4,})\b", line)}
    prose = {"схема", "реальные", "ключи", "перечень", "сверяется", "кодом", "гейтом", "tests",
             "test_outputs_schema_offline", "переписывается", "руками", "канонического", "над",
             "сортированными", "ключами", "числа", "сериализуются", "как", "есть", "без",
             "округления", "поле", "исключается", "имя", "модели", "планировщика", "int", "from",
             "budget", "tracker", "summary", "sorted", "keys", "plan", "brain", "graph", "json",
             "uuid", "sha", "canonical", "over", "with", "numbers", "field", "excluded", "the",
             "and", "top", "level", "real", "list", "checked", "against", "code", "rather", "than",
             "typed", "hand", "name", "model", "planner", "serialised", "rounding", "index"}
    invented = sorted(n for n in named - prose - keys if not n.startswith("test_"))
    if invented:
        fail(f"docs/OUTPUTS.md называет в схеме то, чего код не пишет: {invented}")
    else:
        print("  ok  документ не обещает ни одного ключа сверх написанных кодом")


def main() -> int:
    for fn in (test_the_documented_schema_is_the_written_schema,
               test_the_document_does_not_promise_a_key_the_code_never_writes):
        fn()
    if failures:
        print(f"FAIL — {len(failures)} проблем(а):")
        for f in failures:
            print("  - " + f)
        return 1
    print("outputs schema: OK (перечень ключей выведен из кода и сверен с обеими половинами)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
