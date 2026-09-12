#!/usr/bin/env python3
"""[ENV-SHADOWS-SAVED-SETTING-UNANNOUNCED] — ручка, которую якорь compose ПРЕДЛАГАЕТ, обязана доезжать
до сервисов, которые её должны получить.

ЧТО БЫЛО ЗАМЕРЕНО, И ЧЕМ ЗАПИСЬ РЕЕСТРА ОШИБАЛАСЬ. Запись утверждала: «`x-sentinel-base` вливается в
сервис `control-api`, значит эти имена оказываются в его `os.Environ()`». НЕВЕРНО, и опровергнуто
рендером эффективного конфига (`docker compose config`): собственный блок `environment:` ЗАМЕЩАЕТ
пришедшее по мерж-ключу YAML целиком. Раскомментированные `LLM_*` доезжали до `browser` (своего
блока нет) и НЕ доезжали до `control-api` (блок есть).

Настоящий дефект оказался ГРОМЧЕ названного: `docker-compose.yml` предлагал «uncomment to enable», а
`docker-compose.ghcr.yml` объявлял те же имена ЖИВЫМИ — то есть ручка выглядела уже включённой, — и
в обоих случаях она была мертва ровно для сервиса, который запускает КАЖДЫЙ прогон из интерфейса.
Такой прогон молча уходил на дефолт `anthropic` и офлайн-эвристику.

ПОЧЕМУ ГЕЙТ РАЗБИРАЕТ ТЕКСТ, А НЕ ЗОВЁТ `docker compose config`. Сьют офлайновый, докера в нём нет, а
шаг, который «пропускается при недоступности докера», рапортует успех, когда ничего не проверял.
Предмет проверки — САМ ФАЙЛ, поэтому разбор файла здесь не суррогат: доставка задаётся ровно этими
строками. Рендером эффективного конфига то же самое проверяется в ручной половине приёмки (докер ×3),
и две проверки неизбыточны по замеру: текстовая ловит расхождение без докера, рендер — ошибку в самом
правиле мержа.

Run: .venv/bin/python tests/test_compose_anchor_reaches_services_offline.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("docker-compose.yml", "docker-compose.ghcr.yml")

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  ", name)
    else:
        FAILS.append(name)
        print("  FAIL", name, "\n       ", str(detail)[:500])


def parse(path):
    """Возвращает (имена env якорей, {сервис: (мержит ли якорь, свои имена env)}).

    Разбор построчный и по отступам, потому что загрузчик YAML РАЗРЕШАЕТ мерж-ключ сам и тем самым
    стирает ровно ту величину, которую надо измерить: после загрузки уже не видно, кто мержил якорь.
    """
    anchors, services = set(), {}
    cur, in_env, merges, own = None, False, False, set()
    top = None
    for raw in io.open(path, encoding="utf-8").read().split("\n"):
        if raw.strip().startswith("#") or not raw.strip():
            continue
        if re.match(r"^[a-zA-Z_]", raw):                       # top-level key
            top = raw.split(":")[0]
            in_env = False
            continue
        if top and top.startswith("x-"):                       # an anchor block
            if re.match(r"^  environment:", raw):
                in_env = True
                continue
            if in_env:
                m = re.match(r"^    ([A-Z][A-Z0-9_]*):", raw)
                if m:
                    anchors.add(m.group(1))
                elif re.match(r"^  \S", raw):
                    in_env = False
            continue
        if top != "services":
            continue
        m = re.match(r"^  ([a-z][a-z0-9_-]*):\s*$", raw)       # a service
        if m:
            if cur:
                services[cur] = (merges, own)
            cur, merges, own, in_env = m.group(1), False, set(), False
            continue
        if cur is None:
            continue
        if re.match(r"^    <<:\s*\*", raw):
            merges = True
            continue
        if re.match(r"^    environment:", raw):
            in_env = True
            continue
        if in_env:
            m2 = re.match(r"^      ([A-Z][A-Z0-9_]*):", raw)
            if m2:
                own.add(m2.group(1))
            elif re.match(r"^    \S", raw):
                in_env = False
    if cur:
        services[cur] = (merges, own)
    return anchors, services


def test_every_knob_the_anchor_offers_reaches_the_services_that_replace_it():
    for path in FILES:
        anchors, services = parse(os.path.join(ROOT, path))
        # Полы: сравнение двух пустых множеств сходится идеально.
        check("%s: якорь разобран" % path, len(anchors) >= 5,
              "имён в якоре: %d — разбор отстал от формы файла" % len(anchors))
        check("%s: сервисы разобраны" % path, len(services) >= 4,
              "сервисов: %d" % len(services))
        shadowing = {n: own for n, (merges, own) in services.items() if merges and own}
        check("%s: есть сервис, замещающий якорь своим блоком" % path, bool(shadowing),
              "ни одного — проверка ниже обходит пустое множество")
        for name, own in sorted(shadowing.items()):
            missing = sorted(anchors - own)
            check("%s: сервис %s получает каждую ручку якоря" % (path, name), not missing,
                  "своим блоком `environment:` сервис ЗАМЕЩАЕТ якорь целиком, поэтому %s до него не "
                  "доезжают — ручка предлагается и мертва" % missing)


def main():
    print("[ENV-SHADOWS-SAVED-SETTING-UNANNOUNCED] якорь compose против собственных блоков сервисов")
    test_every_knob_the_anchor_offers_reaches_the_services_that_replace_it()
    if FAILS:
        print("\nFAIL — %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
