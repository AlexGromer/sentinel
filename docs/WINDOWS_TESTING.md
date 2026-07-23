# Инструкция по развёртыванию Sentinel для Windows

> 🌐 **Русский** (основная версия) · [English](WINDOWS_TESTING.en.md)

> **Тип:** How-to · **Аудитория:** оператор live-теста на Windows-хосте
> **Связанные:** [M9_LIVE_PLAN.md](./M9_LIVE_PLAN.md) · [LOCAL_MODELS.md](./LOCAL_MODELS.md) · [QUICKSTART.md](./QUICKSTART.md) · [DISTRIBUTION.md](./DISTRIBUTION.md)

Развёртывание стека Sentinel, запуск проверок M9-LIVE на локальной LLM и сбор артефактов. Обе фазы (встроенные фикстуры и реальные сайты) запускаются через веб-интерфейс - это основной путь для демонстрации; те же прогоны из командной строки вынесены в отдельный раздел. Все команды соответствуют `install.ps1`, `docker-compose.yml` и `M9_LIVE_PLAN.md`. Стек собирается из исходников: `install.ps1` ставит только `agentctl.exe` из релиза с тегом `v*` и для этого теста не подходит.

## Требования к хосту

| Компонент | Значение |
|---|---|
| ОС | Windows 10 или 11 |
| GPU | NVIDIA, 8-12 ГБ VRAM (выбор модели зависит от объёма, см. раздел «Выбор моделей») |
| Docker | Docker Desktop, backend WSL2 |
| Git | Git for Windows (даёт `git` и Git Bash) |
| LLM-рантайм | Ollama для Windows, нативный (прямой доступ к GPU) |

## Как устроена проверка: две фазы и две модели

Sentinel работает в две фазы, и в каждой задействуется своя модель. Модели решают разные задачи в разное время, поэтому в VRAM одновременно не находятся: Ollama грузит их по очереди при `OLLAMA_MAX_LOADED_MODELS=1`. Отсюда и берутся две модели.

| Фаза | Когда работает | Что делает модель | Роль | Модель |
|---|---|---|---|---|
| explore, author | Первый проход: тулз изучает страницу и строит план | Читает дерево страницы (DOM и accessibility), выбирает реальный элемент по индексу, формирует шаги плана | planner, текстовая | `qwen3:14b` |
| replay, heal | Повторный проход готового плана | Если сохранённый локатор сломался из-за смены вёрстки, находит элемент заново, в том числе по скриншоту (set-of-marks) | heal, vision | `qwen2.5-vl:7b` |

Следствия:
- planner никогда не выдумывает селектор: он выбирает индекс среди реально найденных на странице элементов (grounding). Это защищает от галлюцинации локатора.
- heal вызывается, только когда детерминированные стратегии L1-L6 не смогли повторно привязать сломанный локатор. Vision-путь heal сейчас отключён, поэтому основную работу делает planner; vision-модель нужна для будущего visual-heal.
- Фаза replay в норме работает без LLM (0 токенов): planner уже всё решил на этапе explore, а replay воспроизводит замороженный план. LLM включается только на heal.

## Выбор моделей

Нагрузка Sentinel не требует крупной модели: вывод - короткий структурированный JSON (planner propose не более 200 токенов, scenario не более 800, heal не более 200), вход не более 2000 токенов, temperature 0. Цель - минимальная жизнеспособная модель, а не предельная под объём VRAM.

### Базовый набор моделей

| Роль | Модель (12 ГБ) | Модель (8 ГБ) | Примечание |
|---|---|---|---|
| Planner (explore, author) | `qwen3:14b` Q4_K_M, около 9.5 ГБ | `qwen3:8b` Q5, около 6 ГБ | Режим без reasoning. На 8 ГБ 14B частично выгружается в CPU и работает медленно. |
| Heal (replay, vision) | `qwen2.5-vl:7b` Q4, около 7 ГБ | `qwen2.5-vl:7b` Q4 | Поддерживается Ollama, ScreenSpot 84.7. Vision-путь heal пока отключён. |

Точный расчёт под конфигурацию - калькулятор `docs/calculators/vram.html`, методика - `LOCAL_MODELS.md §5`.

### Поиск минимальной модели на M9-LIVE

1. Запусти базовый набор, сними метрики RISK-002 (confidence и доля unmatched на локальных моделях).
2. Повтори тот же тест с planner `qwen3:8b` (параметр `LLM_MODEL_PLANNER=qwen3:8b`). Если grounding и валидность JSON сохраняются, `qwen3:8b` становится рекомендуемым минимумом.
3. Модели у предела VRAM (14B в Q5 или Q6, vision 15B, 32B на большем GPU) применяй, только если данные покажут, что 14B не справляется. Это решение по данным, а не заранее.

Не используй DeepSeek-R1-Distill-14B для planner: reasoning-модель добавляет служебные токены рассуждения, они исчерпывают лимит вывода planner (не более 200 токенов) и вызывают деградацию.

## Установка Ollama и загрузка моделей

Установи нативный Ollama для Windows со страницы https://ollama.com/download/windows

Задай лимит одновременно загруженных моделей. Без него planner и heal вместе превышают объём VRAM, часть модели выгружается в оперативную память, скорость падает:

1. Открой «Изменение системных переменных среды», добавь переменную `OLLAMA_MAX_LOADED_MODELS` со значением `1`.
2. Перезапусти Ollama: значок в трее, «Quit Ollama», затем запусти снова. Переменная сеанса PowerShell на уже запущенный фоновый сервис не действует, нужна системная переменная и перезапуск.

Загрузи модели:
```powershell
ollama pull qwen3:14b
ollama pull qwen2.5-vl:7b
ollama pull qwen3:8b
ollama list
```

## Сборка Sentinel (Docker Desktop)

```powershell
git config --global core.autocrlf input
git clone https://github.com/AlexGromer/sentinel.git
cd sentinel
docker compose build
```

Проверка стека без LLM:
```powershell
docker compose run --rm sentinel run --target "file:///app/testdata/fixtures/l3.html" --planner heuristic --artifact-dir /app/runs/smoke
```
Ожидаемый результат: код возврата 0 и файл `.\runs\smoke\plan.json`. Вывод: стек собран и работает без LLM. Фикстуры l1-l6 уже в образе.

## Рабочий каталог

Все команды `docker compose` выполняй из корня репозитория sentinel (туда ты перешёл командой `cd sentinel`). Compose монтирует относительно корня три каталога: `./runs` (артефакты прогонов, на хосте `.\runs`), `./state` (база разговоров, локаторы, golden-снимки, сокет store-gateway), `./config` (RunConfig YAML). Поэтому артефакты появляются в `.\runs\<id>`, а база чата сохраняется в `.\state\conversations.db` и переживает перезапуск контейнера. Открыл новое окно PowerShell - снова перейди в корень репозитория перед командой.

## Параметры подключения к локальной LLM

Без параметра `LLM_BACKEND=openai` бэкенд по умолчанию anthropic, адрес `LLM_BASE_URL` не учитывается, и запуск без явного сообщения переключается на детерминированный HeuristicPlanner при коде возврата 0. Это главная ошибка вехи: запуск считается успешным при отключённой LLM. Эти переменные задаются сервису control-api (для запусков из UI) и/или контейнеру sentinel (для запусков из командной строки).

| Параметр | Зачем нужен | Откуда взять значение | Варианты | Как задать |
|---|---|---|---|---|
| `LLM_BACKEND` | Выбор клиента LLM | Функция `make_backend` в `brain/llm.py` | anthropic; openai; sampling | `openai` для Ollama |
| `LLM_BASE_URL` | Адрес OpenAI-совместимого эндпоинта | Порт Ollama 11434 | из контейнера `http://host.docker.internal:11434/v1`; нативно `http://localhost:11434/v1` | значение с суффиксом `/v1` |
| `LLM_API_KEY` | Ключ доступа | Ollama ключ не проверяет | любая непустая строка | `noauth` |
| `LLM_MODEL_PLANNER` | Модель роли planner | Список `ollama list` | `qwen3:14b`; `qwen3:8b` | точное имя тега |
| `LLM_MODEL_HEAL` | Модель роли heal | Список `ollama list` | `qwen2.5-vl:7b` | точное имя тега |
| `LLM_VISION` | Vision у роли heal | Нужна vision-модель | `1` включить; пусто выключить | `1` для `qwen2.5-vl` |
| `LLM_STRUCTURED` | Строгий структурированный вывод (ADR-057) | Опционально | `1` включить; пусто выключить | включать после проверки поддержки `json_schema` эндпоинтом |
| `OLLAMA_MAX_LOADED_MODELS` | Лимит моделей в памяти | Системная переменная Windows | `1` | значение `1`, затем перезапуск Ollama |

Значения `LLM_MODEL_PLANNER` и `LLM_MODEL_HEAL` переопределяют общий `LLM_MODEL` для конкретной роли. Имена берутся из `brain/llm.py`, форма `SENTINEL_..._MODEL` не поддерживается.

## Запуск через веб-интерфейс

Это основной путь: прогоны настраиваются и запускаются из браузера, шаги видны на живом timeline. Обе фазы ниже выполняются так. Нужны два сервиса: `control-api` (запускает прогоны по HTTP, слушает `127.0.0.1:8090`) и `webui` (страницы на порту 8088).

Запусти оба сервиса. control-api нужен только токен доступа - LLM-подключение задаётся в самом UI (ADR-063):
```powershell
$env:CONTROL_API_TOKEN = "demo-token"                     # включает запуск прогонов из UI; без токена API только на чтение
docker compose --profile control-api up -d control-api    # 127.0.0.1:8090
docker compose --profile webui up -d webui                # http://localhost:8088
```

Открой хаб `http://localhost:8088/`. Это co-pilot (ADR-055) с разделами Settings (подключение и конфигуратор прогона) и Tests (библиотека сценариев и тестов, история прогонов, разговоры, живой AG-UI timeline с auto-HITL).

1. В Settings укажи адрес control-api `http://localhost:8090` и токен `demo-token`.
2. В разделе #build задай LLM-подключение: backend `openai`, base_url `http://host.docker.internal:11434/v1`, модель planner `qwen3:14b`, модель heal `qwen2.5-vl:7b`, vision по необходимости. Эти поля уходят с прогоном, и control-api материализует их в env (ADR-063) - отдельно прописывать `LLM_*` в окружение control-api не нужно. Ключ для локального Ollama не требуется (control-api подставляет `noauth`).
3. На каждый прогон задаёшь target (мишень), goal (цель на естественном языке) и режим (обычно `goal`), жмёшь Run и смотришь живой timeline; вердикт и артефакты появляются там же, прогон попадает в историю Tests с идентификатором вида `control-<...>`.

Приоритет источников LLM: **process env control-api > per-run из UI > persisted-конфиг**. То есть если очень нужно зафиксировать модель на стороне control-api, задай `LLM_*` в его окружении - тогда UI её не переопределит. Мастер `http://localhost:8088/setup/` собирает те же поля в форму и кнопкой «Сохранить на сервер» пишет их в persisted-конфиг (нужен профиль `store`, см. «База данных и чат-режим»). Чат - на `http://localhost:8088/chat/`.

## Как читать артефакты прогона

Прогон из UI создаёт каталог `runs\control-<id>\` (из командной строки - `runs\<id>\` по твоему `--artifact-dir`). В UI итог виден на timeline и в истории Tests; те же данные лежат в файлах:

| Файл | Что содержит | Что проверять |
|---|---|---|
| `plan.json` | Замороженный план шагов и `plan_hash` | Шаги ссылаются на реальные элементы; при replay `plan_hash` не должен меняться |
| `scenario.json` | Сценарий для goal и describe режимов | Привязка шагов к элементам, поле unmatched |
| `heal-report.json` | Отчёт восстановления локаторов | Стратегия (L1-L6), confidence, healed и failed |
| `llm-transcript.jsonl` | Лог обращений к LLM | Поле `planner` равно `llm`, а не `heuristic` |
| `report.json`, `report.html` | Итог прогона и код возврата | Поле `exit_code` |
| `metrics.prom` | Метрики Prometheus | `sentinel_run_exit_code` |
| `trace.zip` | Playwright-трейс (живой DOM и тела запросов) | Только для локального разбора, наружу не отдаётся |

Коды возврата (из `brain/replay.py` и `brain/__main__.py`):

| Код | Значение | Трактовка |
|---|---|---|
| 0 | pass | Прогон прошёл успешно |
| 1 | step failure | Провалился шаг (нерасквоттированный) |
| 2 | golden regression | Расхождение с golden-baseline (нерасквоттированное) |
| 3 | plan integrity | Расхождение `plan_hash` или неверный вызов, жёсткий обрыв |

## Фаза 1: прогон на встроенных фикстурах l1-l6

Фикстуры - самодостаточные HTML-страницы, доступны контейнеру по адресу `file://` (они в образе). Каждую запускай через веб-интерфейс: в Settings вставь target и goal, выбери режим `goal`, нажми Run и наблюдай timeline. Проходи по возрастанию сложности. Для каждой ниже: что задать в UI, ожидаемый результат, какой вывод. После каждого прогона в его записи проверяй, что использовалась LLM (`planner` = `llm`).

### L1: обнаружение элементов
Что проверяет: обнаружение кнопок, кликов и якорных ссылок (4 кнопки, одна disabled, anchor-ссылки).
- Target: `file:///app/testdata/fixtures/l1.html`
- Goal: `нажми основную кнопку, затем перейди по якорной ссылке в раздел`
- Режим: `goal`

Ожидаемый результат: на timeline шаги указывают на реальные кнопки и ссылки, disabled-кнопка не выбрана; вердикт 0. Вывод: planner видит элементы страницы и не выдумывает селектор.

### L2: логин
Что проверяет: заполнение и вход, ветки правильных и неправильных кред. Демо-креды `demo` / `demo`.
- Target: `file:///app/testdata/fixtures/l2.html`
- Goal: `войди с логином demo и паролем demo и подтверди появление панели входа`
- Режим: `goal`

Ожидаемый результат: на timeline появляется `#panel-logged-in`, ошибки `#alert-error` нет; вердикт 0. Вывод: заполнение полей и клик по кнопке входа работают, состояние после входа распознано.

### L3: валидация формы
Что проверяет: негативные проверки, per-field ошибки (email-формат, число 18-120, required, максимум 80 символов со счётчиком).
- Target: `file:///app/testdata/fixtures/l3.html`
- Goal: `заполни форму валидными данными и отправь, затем проверь ошибку при неверном email и числе вне диапазона 18-120`
- Режим: `goal`

Ожидаемый результат: при валидных данных форма принимается, при неверных появляются `#err-*` рядом с полями; вердикт 0. Вывод: тулз различает валидные и невалидные состояния и читает per-field ошибки.

### L4: многостраничный флоу
Что проверяет: сквозной сценарий на 3 страницы с передачей через sessionStorage и модалкой подтверждения. Демо-креды `admin` / `secret`.
- Target: `file:///app/testdata/fixtures/l4.html`
- Goal: `войди с логином admin и паролем secret, перейди в дашборд, открой биллинг и подтверди апгрейд в модальном окне`
- Режим: `goal`

Ожидаемый результат: на timeline план проходит `l4.html`, затем `l4-dashboard.html`, затем `l4-billing.html`, подтверждение в модалке нажато; вердикт 0. Вывод: тулз ведёт многошаговый бизнес-сценарий с навигацией между страницами.

### L5: табы и shadow DOM
Что проверяет: ARIA-табы, асинхронную инъекцию контента (через 600 мс) и элементы в shadow DOM (RISK-005).
- Target: `file:///app/testdata/fixtures/l5.html`
- Goal: `переключись на вкладку с динамическим контентом, дождись загрузки элементов, затем открой палитру цвета`
- Режим: `goal`

Ожидаемый результат: `#dynamic-slot` дожидается замены на реальный контент, элемент в shadow DOM найден через pierce-локатор; вердикт 0. Вывод: тулз работает с асинхронным контентом и shadow DOM, а не только со статикой.

### L6: несколько вкладок браузера
Что проверяет: отслеживание новых вкладок браузера (`target=_blank` и `window.open`) плюс внутренние ARIA-табы.
- Target: `file:///app/testdata/fixtures/l6-newtab.html`
- Goal: `открой ссылку в новой вкладке и переключись на неё, затем переключи внутренние табы`
- Режим: `goal`

Ожидаемый результат: новые вкладки отражены в `browser.tabs`, переключение между ними выполнено; вердикт 0. Вывод: тулз видит и переключает вкладки браузера и внутренние табы.

### Проверка self-heal на фикстурах
1. Запусти прогон L2 через UI (как выше) - в истории Tests появится план.
2. Повтори тот же план против изменённой версии страницы: в разделе Tests выбери прогон и нажми Re-run (это replay), либо задай target изменённой копии фикстуры (с переименованным id или другим порядком элементов).

Ожидаемый результат: если локатор сломался, в записи прогона (`heal-report.json`) видна стратегия L1-L6 и confidence; при успешном восстановлении вердикт 0. Вывод: детерминированный heal чинит сломанный локатор, срабатывает confidence-gate (данные для RISK-002).

## Фаза 2: прогон на реальных публичных сайтах

Направляй Sentinel только на публичные песочницы, созданные для автоматизации, либо на приложения, которыми владеешь сам. Никогда не направляй на чужие продакшн-сайты: даже read-only обход DOM чужого сайта - риск нарушения условий использования и несанкционированного доступа. Все сайты ниже - публичные тренировочные песочницы (Sauce Labs и другие). Перед первым прогоном убедись, что сайт доступен. Вводи только опубликованные тестовые креды, никогда не вводи реальные личные, платёжные или учётные данные. Каждый сайт запускается так же, через UI: target - это `https`-адрес сайта, Chromium уже в образе.

### Сайт 1: простой логин (Practice Test Automation)
Адрес и креды: https://practicetestautomation.com/practice-test-login/ , `student` / `Password123` (опубликованы на странице).
- Target: `https://practicetestautomation.com/practice-test-login/`
- Goal: `войди с логином student и паролем Password123, нажми Submit и подтверди сообщение об успешном входе`
- Режим: `goal`

Ожидаемый результат: переход на `/logged-in-successfully/`, заголовок «Congratulations student. You successfully logged in!» и кнопка «Log out»; вердикт 0. Вывод: базовый логин на реальном сайте работает. Негативные фикстуры на той же форме: неверный логин даёт «Your username is invalid!», неверный пароль - «Your password is invalid!». Примечание: у сайта есть анти-бот-стена для простых HTTP-клиентов; Sentinel ходит настоящим браузером Chromium и проходит нормально.

Тест self-heal: страница статична, поэтому используй контролируемый снос. Заземли кнопку Submit по id `#submit`, затем в replay подставь устаревший или переименованный id. Heal должен повторно привязаться по видимому тексту «Submit» или роли button и всё равно отправить форму.

### Сайт 2: многостраничный checkout (SauceDemo)
Адрес и креды: https://www.saucedemo.com/ , `standard_user` / `secret_sauce`.
- Target: `https://www.saucedemo.com/`
- Goal: `войди как standard_user с паролем secret_sauce, добавь в корзину товар Sauce Labs Backpack, открой корзину, оформи заказ с любыми именем, фамилией и индексом, нажми Finish и подтверди завершение заказа`
- Режим: `goal`

Ожидаемый результат: на timeline шесть последовательных экранов (login, inventory, cart, checkout-step-one, checkout-step-two, checkout-complete), финальная страница `/checkout-complete.html` с заголовком «THANK YOU FOR YOUR ORDER» и кнопкой «Back Home»; вердикт 0. Вывод: тулз ведёт полный многостраничный сценарий e-commerce. Дополнительные аккаунты (пароль тот же): `locked_out_user` для негативного теста входа; `problem_user`, `performance_glitch_user`, `error_user`, `visual_user` для проверки устойчивости к багам и задержкам UI.

Тест self-heal: заземли кнопку «Add to cart» рюкзака по позиционному селектору (индекс в сетке товаров), затем поменяй «Sort by» на Name Z-A или Price high-low. Переупорядочивание сдвигает товар на другой индекс и ломает позиционный локатор, а стабильный атрибут `data-test` (`add-to-cart-sauce-labs-backpack`) и видимое имя товара остаются. Хороший heal повторно привязывается по `data-test` или имени товара, а не по позиции.

### Сайт 3: динамические виджеты и стресс self-heal (The Internet)
Адрес и креды: https://the-internet.herokuapp.com/ , форма входа на `/login`: `tomsmith` / `SuperSecretPassword!`. Виджет-страницы кред не требуют.
- Target: `https://the-internet.herokuapp.com/`
- Goal: `на /login войди как tomsmith с паролем SuperSecretPassword! и подтверди баннер успеха, затем на /checkboxes отметь первый чекбокс, на /dropdown выбери Option 2, на /add_remove_elements добавь элемент и удали его, на /dynamic_loading/1 нажми Start и дождись текста Hello World`
- Режим: `goal`

Ожидаемый результат: вход ведёт на `/secure` с текстом «You logged into a secure area!»; первый чекбокс отмечен; в dropdown выбран Option 2; после удаления добавленный элемент исчез; на `/dynamic_loading/1` появился «Hello World!» после асинхронной задержки; вердикт 0. Вывод: тулз работает с динамическими виджетами и асинхронным контентом. Примечание: бесплатный Heroku-dyno может засыпать, первый запрос бывает медленным - это задержка, а не отказ.

Тест self-heal: страница `/challenging_dom` перегенерирует id и классы при каждой загрузке, поэтому сохранённый локатор по id или классу ломается на следующем прогоне, а видимый текст и роль остаются. Это чистая проверка восстановления по тексту или роли. Проверь эмпирически reload-диффом перед использованием как эталон.

### Сайт 4 (опционально): формы регистрации (Automation Exercise)
Адрес: https://automationexercise.com . Фиксированных кред нет: на каждый прогон регистрируй новый уникальный email (с суффиксом-временем), иначе будет «email already exists».
- Target: `https://automationexercise.com`
- Goal: `зарегистрируй новый аккаунт с уникальным email, заполни форму данных фиктивными значениями, добавь любой товар в корзину, оформи заказ, оплати фиктивными данными карты и подтверди подтверждение заказа`
- Режим: `goal`

Ожидаемый результат: финальная страница показывает «Congratulations! Your order has been confirmed!»; вердикт 0. Вывод: тулз проходит регистрацию и три формы (регистрация, данные аккаунта, оплата). Ограничения (почему опционально): нет фиксированных кред и каталог со временем меняется, поэтому проверки не должны опираться на конкретное имя товара или цену. Оплата принимает только фиктивные цифры карты.

## База данных и чат-режим

Многоходовой диалог (chat) сохраняет контекст между ходами в базе данных, поэтому второй и последующие ходы работают поверх уже построенной карты сайта.

Хранилище по умолчанию - SQLite в каталоге `./state` (смонтирован compose): разговоры в `state/conversations.db`, локаторы и golden-снимки там же. Персистентность обеспечивает монтирование `./state`, поэтому диалоги переживают перезапуск контейнера. Postgres (опционально, для нескольких раннеров или K3s): задай переменную `CHECKPOINT_DSN=postgresql://user:pass@host:5432/sentinel` сервису, и чекпойнтер разговоров переключается с SQLite на Postgres (`langgraph PostgresSaver`). Store-gateway для доменов runs, scenarios, tests, results, metrics пока SQLite; Postgres для него - отдельный сервис M13, впереди.

Чат через веб-интерфейс: открой `http://localhost:8088/chat/`. Консоль сама генерирует `conversation_id`, тред накапливается между ходами, кнопка «Новый разговор» начинает новый. Под капотом control-api по `POST /v1/runs` с полем `conversation_id` запускает `agentctl run --mode chat --conversation-id`. Веди диалог в несколько ходов по одной цели (например: «войди как demo/demo», затем «теперь нажми выход») - второй ход работает поверх карты сайта из первого.

Проверка персистентности из командной строки (два хода одним `conversation-id`):
```powershell
$LLM = @("-e","LLM_BACKEND=openai","-e","LLM_BASE_URL=http://host.docker.internal:11434/v1","-e","LLM_API_KEY=noauth","-e","LLM_MODEL_PLANNER=qwen3:14b","-e","LLM_MODEL_HEAL=qwen2.5-vl:7b")
docker compose run --rm $LLM sentinel run --mode chat --conversation-id demo-conv-1 --goal "войди как demo с паролем demo" --target "file:///app/testdata/fixtures/l2.html" --artifact-dir /app/runs/chat1
docker compose run --rm $LLM sentinel run --mode chat --conversation-id demo-conv-1 --goal "теперь нажми кнопку выхода" --target "file:///app/testdata/fixtures/l2.html" --artifact-dir /app/runs/chat2
```
Проверка: в логе первого хода - строка `chat: COLD conversation=demo-conv-1`, во втором - `chat: RESUME conversation=demo-conv-1`. Файл `state\conversations.db` не очищается в конце хода. Вывод: диалог ведётся с сохранением контекста в БД. Сохранённые разговоры также доступны через `GET /v1/chats` у control-api (нужен токен), а любой OpenAI-совместимый клиент может вести Sentinel «как модель» через `POST /v1/chat/completions` (один ход чата = один прогон).

## Проверка того, что использовалась LLM

Открой файл `.\runs\<id>\llm-transcript.jsonl` (для прогона из UI - `.\runs\control-<id>\llm-transcript.jsonl`), найди поле `planner`: значение должно быть `llm`, а не `heuristic`.
```powershell
Select-String -Path .\runs\*\llm-transcript.jsonl -Pattern '"planner"' | Select-Object -First 3
```
На локальных моделях 14B и 7B доля FLAG и unmatched выше, чем на облачных Opus и Sonnet: пороги AUTO 0.85 и FLAG 0.60 настраивались на облачных моделях. Это ожидаемые данные для калибровки RISK-002 и RISK-003, а не ошибка. Записывай по каждому запуску в файл `runs\LIVE_NOTES.md`: идентификатор, модель, мишень, ожидаемый и полученный результат, код возврата, замеченные отклонения.

## Сбор и передача артефактов

`<run_id>` - это имя каталога под `runs`: для прогона из UI это `control-<...>` (виден в истории Tests), для прогона из командной строки - имя из `--artifact-dir`. Список готовых прогонов: `dir runs` в PowerShell или `ls runs` в Git Bash.

Собери КАЖДЫЙ прогон, который нужно разобрать (запускай в Git Bash или WSL):
```bash
for id in $(ls runs | grep -vx LIVE_NOTES.md); do
  scripts/collect-live-run.sh "$id"
done
scripts/collect-live-run.sh <run_id> --with-trace   # опционально: + trace.zip, не редактируется, только разовый стенд
```
Каждый вызов создаёт `live-results/live-<id>.tar.gz`. Редакция включена по умолчанию и применяется к промежуточной копии, каталог `runs/` не меняется: значения шагов fill, type, select, press без `secretRef` обнуляются, заголовки Authorization, Bearer, Cookie и строки вида `sk-` и JWT удаляются. Хеши, идентификаторы и счётчики (`plan_hash`, golden-sha256) сохраняются. Файлы `checkpoint.db` и `storage_state*.json` не собираются никогда. База разговоров `state/conversations.db` лежит вне `runs/<id>/` и в бандл не попадает; для разбора чата достаточно артефактов `runs/chat1` и `runs/chat2`.

Что передать: все файлы `live-results/live-*.tar.gz` плюс твой журнал `runs/LIVE_NOTES.md` (его collect-скрипт в бандл не кладёт, копируй отдельно). Перенос: USB или scp, не через git (`.gitignore` без сообщения проглатывает `*.tar.gz`, gitleaks внутрь gzip не смотрит). Положи всё на dev-хост в каталог `/opt/agent_development/live-results/`, затем сообщи «разбери live-прогоны».

## Доступ по сети

Для запуска на фикстурах и на реальных сайтах адрес хоста не нужен: всё выполняется локально. Веб-интерфейс (порт 8088) и control-api (`127.0.0.1:8090`) открываются с того же хоста по `localhost`. Адрес хоста в локальной сети (узнать командой `ipconfig`) нужен, чтобы обратиться к сервисам с другого компьютера:

- Открыть веб-интерфейс с другого хоста: `http://<адрес-хоста>:8088`. Проверь правила брандмауэра Windows. control-api по умолчанию слушает только `127.0.0.1:8090`, поэтому для доступа к API с другого хоста его порт нужно опубликовать отдельно и добавить origin в переменную `CONTROL_API_CORS_ORIGINS`.
- Обращаться к Ollama с другого хоста: задай системную переменную `OLLAMA_HOST=0.0.0.0`, перезапусти Ollama, затем укажи `LLM_BASE_URL=http://<адрес-хоста>:11434/v1`. Это открывает Ollama в локальной сети без авторизации, применяй только в доверенной сети.

## Особенности Windows

- `host.docker.internal` - адрес, по которому контейнер обращается к нативному Ollama на Windows-хосте. Если Ollama запущена как сервис compose (профиль ollama), адрес из контейнера равен `http://ollama:11434/v1`.
- Пути тома: в PowerShell `${PWD}\runs:/app/runs`, в CMD `%cd%\runs:/app/runs`.
- GPU: нативный Ollama для Windows использует GPU напрямую; проброс GPU в Ollama внутри Docker на Windows сложнее, поэтому берётся нативный.
- Окончания строк: bash-скрипты требуют LF, поэтому до клонирования задай `git config --global core.autocrlf input`, либо работай в WSL.

## Запуск из командной строки (альтернатива)

Те же прогоны можно запускать без UI. Набор `$LLM` - из раздела «Параметры подключения»:
```powershell
$LLM = @(
  "-e","LLM_BACKEND=openai",
  "-e","LLM_BASE_URL=http://host.docker.internal:11434/v1",
  "-e","LLM_API_KEY=noauth",
  "-e","LLM_MODEL_PLANNER=qwen3:14b",
  "-e","LLM_MODEL_HEAL=qwen2.5-vl:7b",
  "-e","LLM_VISION=1"
)
docker compose run --rm $LLM sentinel run --goal "<цель>" --target "<мишень>" --artifact-dir /app/runs/l1
docker compose run --rm $LLM sentinel run --replay --plan /app/runs/l1/plan.json --artifact-dir /app/runs/l1-replay
```
Подставь target и goal из Фазы 1 и Фазы 2. Здесь ты сам задаёшь `--artifact-dir`, поэтому `<run_id>` равен этому имени (в UI идентификатор генерируется как `control-<...>`).

## Альтернатива: сборка в WSL2 (Ubuntu)

```bash
sudo apt update && sudo apt install -y golang-1.26 nodejs npm python3 git
curl -LsSf https://astral.sh/uv/install.sh | sh
go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway && go build -o bin/control-api ./cmd/control-api
cd pw-executor && npm i && npm run build && npx playwright install chromium && cd ..
cd brain && UV_PROJECT_ENVIRONMENT=../.venv uv sync --frozen && cd ..
export LLM_BACKEND=openai LLM_BASE_URL=http://localhost:11434/v1 LLM_API_KEY=noauth LLM_MODEL_PLANNER=qwen3:14b LLM_MODEL_HEAL=qwen2.5-vl:7b
bin/agentctl run --goal "войди с логином demo и паролем demo" --target "file://$PWD/testdata/fixtures/l2.html" --artifact-dir runs/l2
```
Виртуальное окружение Python строго в корне репозитория (`UV_PROJECT_ENVIRONMENT=../.venv`), иначе agentctl использует системный python3 и завершается с ошибкой.

## Критерии приёмки M9-LIVE

- [ ] Фаза 1: explore и author через веб-интерфейс проходят на l1-l6 (grounded, вердикт 0, поле `planner` равно `llm`).
- [ ] Фаза 2: логин, checkout и виджет-сценарий проходят через веб-интерфейс хотя бы на трёх реальных сайтах.
- [ ] прогон настроен и наблюдаем в UI: подключение к control-api, живой timeline, прогон в истории Tests.
- [ ] heal чинит расхождение с корректным confidence-gate, без ложного авто-heal.
- [ ] чат в `/chat/`: второй ход даёт RESUME того же conversation-id, разговор сохранён в `state\conversations.db`.
- [ ] golden байтово стабилен дважды (RISK-009).
- [ ] лимит бюджета срабатывает с корректной деградацией.
- [ ] собраны реальные значения RISK-002 (confidence) и RISK-003 (стоимость и задержка).
