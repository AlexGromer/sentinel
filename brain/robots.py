"""Воля владельца чужого сайта, прочитанная машиной (ADR-133).

⚠ ЗАЧЕМ. Инструмент ходит по чужим сайтам и до сих пор не знал про `robots.txt` вовсе: фронтир
фильтровался только границей `base_origin` (`brain/graph.py`, узел `ground`). Замерено 2026-08-23 на
`practice.expandtesting.com`, чьё разрешение на использование подтверждено дословно: его `robots.txt`
говорит `Allow: /` и следом СЕМЬ `Disallow` — `/notes/api/*`, `/download-secure`, `/digest-auth`,
`/download/*`, `/infinite-scroll/*`, `/__/`. С главной страницы ведут 96 внутренних ссылок, ТРИ из
них — в запрещённые пути. Разрешение владельца читал человек, и до кода оно не доезжало.

⚠ ЧТО ДЕЛАЕТСЯ С НЕДОСТИЖИМЫМ `robots.txt`, и почему не «запретить всё». RFC 9309 §2.3.1 разрешает
краулеру считать 5xx полным запретом, и для поисковика это верно: он ходит по миллиону чужих сайтов,
и цена ошибки — чужой трафик. Здесь цена другая и она замерена по назначению продукта: человек
наводит инструмент на СВОЁ приложение, и стенд, чей `robots.txt` отдал 500, получил бы отказ обходить
собственный сайт — с сообщением про чужую волю, которой никто не выражал. Поэтому недостижимость
РАЗРЕШАЕТ обход и ГРОМКО об этом говорит: `warn` в журнал и запись в артефакт, что правила прочитать
не удалось. Тихо разрешить — вот чего делать нельзя; это и было бы «мы не знаем, но сделали вид».

⚠ ПОЧЕМУ ИСКЛЮЧЁННОЕ ПИШЕТСЯ В АРТЕФАКТ. Без перечня «исключено» неотличимо от «там ничего нет»:
человек, открывший карту сайта, увидит отсутствие раздела и решит, что обход его не нашёл. Это ровно
тот класс, который ADR-131 закрывал блоком `completeness` — факт есть, читателя нет.
"""
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .eventlog import log

# Имя, под которым мы себя объявляем в правилах. Совпадений с ним в чужих `robots.txt` не бывает
# практически никогда, и это НОРМАЛЬНО: `RobotFileParser` откатывается на группу `*`, то есть на
# правила «для всех», а они и есть воля владельца, адресованная неизвестному агенту.
USER_AGENT = "Sentinel"

_TIMEOUT_S = 8


class _Rules:
    """Правила одной группы `robots.txt` по RFC 9309 — САМОЕ ДЛИННОЕ совпадение выигрывает.

    ⚠ ПОЧЕМУ НЕ `urllib.robotparser`, ХОТЯ ОН В СТАНДАРТНОЙ БИБЛИОТЕКЕ. Он реализует ПЕРВОЕ
    совпадение по порядку строк, а не самое длинное, и на форме

        User-agent: *
        Allow: /
        Disallow: /download-secure

    отвечает «разрешено» для `/download-secure`: строка `Allow: /` стоит выше и матчит всё.
    ЗАМЕРЕНО: `RobotFileParser.can_fetch('Sentinel', 'https://t.example/download-secure')` → `True`.
    Это не редкая форма — это ровно тот файл, который вскрыл нужду в этой работе
    (`practice.expandtesting.com`: `Allow: /` и следом семь `Disallow`). Взять готовое и получить
    «разрешено» на каждом запрете было бы хуже, чем не читать `robots.txt` вовсе: инструмент
    утверждал бы, что волю владельца соблюдает.

    RFC 9309 §2.2.2: выигрывает правило с самым длинным совпавшим шаблоном; при равной длине
    выигрывает `Allow`. Поддержаны `*` (любая последовательность) и `$` (конец пути).

    ⚠ ЧЕГО ЗДЕСЬ НЕТ, сказано прямо: процентное декодирование путей и сравнение регистронезависимых
    хостов. Оба относятся к нормализации АДРЕСА, а не к выбору правила, и ни один замер этой волны их
    не потребовал; при встрече — добавлять с замером, а не на всякий случай.
    """

    __slots__ = ("allow", "disallow")

    def __init__(self):
        self.allow, self.disallow = [], []

    @staticmethod
    def _rx(pattern: str):
        import re
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return re.compile("^" + rx + ("$" if anchored else ""))

    def add(self, kind: str, pattern: str) -> None:
        if not pattern:
            # `Disallow:` с пустым значением означает «ничего не запрещено» — это НЕ запрет корня,
            # и путать их значило бы закрыть весь сайт по строке, разрешающей всё.
            return
        (self.allow if kind == "allow" else self.disallow).append((len(pattern), self._rx(pattern)))

    def allows(self, path: str) -> bool:
        best_allow = max((n for n, rx in self.allow if rx.match(path)), default=-1)
        best_deny = max((n for n, rx in self.disallow if rx.match(path)), default=-1)
        # Равная длина — за разрешение (RFC 9309 §2.2.2). Отсутствие обоих — тоже.
        return best_allow >= best_deny


def _parse(text: str, agent: str) -> "tuple[_Rules, int]":
    """Выбрать группу для нашего агента и вернуть (правила, число запретов в файле).

    Группа ищется по САМОМУ ДЛИННОМУ совпадению имени (RFC 9309 §2.2.1); `*` — запасная. Число
    запретов считается по ВСЕМУ файлу и уезжает в артефакт как «сколько правил у владельца», а не
    «сколько применилось к нам» — второе без первого читается как «правил почти нет»."""
    groups: "dict[str, _Rules]" = {}
    current: "list[str]" = []
    fresh = True
    total_disallow = 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not fresh:
                current = []
            current.append(value.lower())
            groups.setdefault(value.lower(), _Rules())
            fresh = True
            continue
        if key in ("allow", "disallow"):
            fresh = False
            if key == "disallow" and value:
                total_disallow += 1
            for name in current:
                groups[name].add(key, value)
    me = agent.lower()
    best = ""
    for name in groups:
        if name != "*" and me.startswith(name) and len(name) > len(best):
            best = name
    chosen = groups.get(best) or groups.get("*") or _Rules()
    return chosen, total_disallow


class RobotsPolicy:
    """Решение «можно ли туда идти» плюс всё, что об этом надо СКАЗАТЬ."""

    __slots__ = ("respected", "source", "detail", "detail_ru", "rules", "_rp")

    def __init__(self, respected, source, detail, detail_ru, rules=0, rp=None):
        self.respected = respected      # соблюдаем ли мы правила в этом прогоне
        self.source = source            # fetched | absent | unreachable | not_applicable | ignored
        # ⚠ ДВЕ ПОЛОВИНЫ, А НЕ ОДНА. Эту фразу наш код авторит ЦЕЛИКОМ, значит по правилу репозитория
        # она переводится — иначе английский читатель получает русский текст в артефакте, минуя
        # каталог, который для того и есть. Форма скопирована с `readyz` (`Detail`/`DetailRU`), где
        # ту же границу уже провели в W6.
        self.detail = detail            # en
        self.detail_ru = detail_ru
        self.rules = rules              # сколько запретов относится к нам
        self._rp = rp

    def allows(self, url: str) -> bool:
        if not self.respected or self._rp is None:
            return True
        try:
            sp = urlsplit(url)
            path = sp.path or "/"
            if sp.query:
                path += "?" + sp.query
            return self._rp.allows(path)
        except Exception as e:
            # Разбор чужого файла не должен ронять прогон: непрочитанное правило — это «не знаем»,
            # а «не знаем» здесь разрешает (см. верх модуля). Но МОЛЧА разрешать нельзя: это
            # единственный путь, на котором инструмент утверждает, что волю владельца соблюдает, и
            # при этом её не применяет. Один код на адрес — не спам: шаблоны компилируются при
            # разборе файла, поэтому сюда попадает разве что диковинный URL.
            log("run.robots_check_failed", url=url, error=e)
            return True

    def as_artifact(self, excluded) -> dict:
        return {"respected": bool(self.respected), "source": self.source,
                "detail": self.detail, "detail_ru": self.detail_ru, "rules": int(self.rules),
                "excluded": list(excluded or [])}


def _fetch(url: str) -> "tuple[str|None, str]":
    """Вернуть (текст, причина). Текст None означает, что правил у нас нет."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            return r.read(512 * 1024).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        # 4xx — «файла нет», и по RFC 9309 это означает «разрешено всё». Отдельная ветка, потому что
        # это ЗАКОННЫЙ ответ, а не сбой, и путать его со сбоем значило бы пугать читателя на каждом
        # втором сайте.
        return None, ("absent" if 400 <= e.code < 500 else f"unreachable:HTTP {e.code}")
    except Exception as e:
        return None, f"unreachable:{type(e).__name__}"


def load(target: str, *, ignore: bool = False, fetch=None) -> RobotsPolicy:
    """Прочитать `robots.txt` цели. `fetch` подменяется в гейтах — сети в сьюте нет по устройству."""
    if ignore:
        # Громко и один раз: человек выбрал это САМ, и запись об этом обязана пережить прогон.
        log("run.robots_ignored", target=target)
        return RobotsPolicy(False, "ignored",
                            "the robots.txt rules were not respected: asked for with --ignore-robots",
                            "правила robots.txt не соблюдались: запрошено флагом --ignore-robots")
    sp = urlsplit(target or "")
    if sp.scheme not in ("http", "https"):
        return RobotsPolicy(True, "not_applicable",
                            f"the {sp.scheme or '(none)'} scheme has no robots.txt — nothing to respect",
                            f"у схемы {sp.scheme or '(нет)'} нет robots.txt — соблюдать нечего")
    url = f"{sp.scheme}://{sp.netloc}/robots.txt"
    text, why = (fetch or _fetch)(url)
    if text is None:
        if why == "absent":
            log("run.robots_absent", url=url)
            return RobotsPolicy(True, "absent", f"{url} was not served by the site — there are no rules",
                                f"{url} не отдан сайтом — запретов нет")
        log("run.robots_unreachable", url=url, error=why.split(":", 1)[-1])
        return RobotsPolicy(True, "unreachable",
                            f"{url} could not be read ({why.split(':', 1)[-1]}) — the crawl was NOT "
                            f"restricted, and that does not mean there are no rules",
                            f"{url} прочитать не удалось ({why.split(':', 1)[-1]}) — обход НЕ "
                            f"ограничивался, и это не значит, что запретов нет")
    rp, rules = _parse(text, USER_AGENT)
    log("run.robots_applied", url=url, rules=rules)
    return RobotsPolicy(True, "fetched", f"{url} was read; disallow rules in the file: {rules}",
                        f"{url} прочитан, запретов в файле: {rules}", rules, rp)


def from_env(target: str) -> RobotsPolicy:
    """Точка входа прогона: флаг доезжает переменной, как и все остальные."""
    return load(target, ignore=os.environ.get("IGNORE_ROBOTS", "0") == "1")
