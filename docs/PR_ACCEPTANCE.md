# Перечень приёмки PR — Sentinel

> 🌐 **Русский** (основная версия) · [English](PR_ACCEPTANCE.en.md)

Что должно быть исполнено, прежде чем PR мержится. Половина исполняется CI и здесь только
**описана**; вторая половина по устройству не автоматизируется, и у каждой её строки записана
причина — не как оправдание, а потому что иначе пропуску негде объявиться.

Нормативная ссылка: `docs/DEVELOPMENT.md` §0, принцип 7 — PR, добавляющий компонент или
функциональность, обязан расширить наблюдение за ним. До появления этого файла принцип ссылался на
«матрицу проверок», которой в репозитории не существовало: единственное вхождение этого сочетания
было в самой формулировке принципа.

**Почему не «матрица».** Слово занято четырежды: `docs/M16_MATRIX.md` (замер разрыва CLI↔UI),
`strategy.matrix` самого CI, релизная матрица платформ в `docs/DISTRIBUTION.md` и parity-матрица
`docs/M14_CONTRACT.md`. Человек, которому сказали «покрой в матрице», открывал бы документ о
вердиктах ui-missing. «Приёмка» уже принадлежит этому смыслу — `docs/DEVELOPMENT.md` §4 «Гейты вех
(приёмка)» — и ничем другим не занята. Имя `PR_GATES.md` отклонено: «гейт» в этом репозитории
означает автоматическую проверку, а половина перечня по устройству ручная, и имя обещало бы
автоматизацию, которой нет.

---

## 1. Машинная половина — это гоняет CI

Строки ниже **сверяются с `.github/workflows/ci.yml`** гейтом `tests/test_pr_acceptance_offline.py`:
каждая обязана назвать существующую джобу и существующий шаг, а множество названных джоб обязано
совпасть с множеством джоб файла **в обе стороны**. Документированная проверка, которой не гоняет ни
один workflow, и джоба, о которой документ молчит, — оба случая красные.

Ничего из перечисленного вручную делать не нужно: это то, что происходит само, и здесь оно записано
затем, чтобы ручная половина ниже читалась как **остаток**, а не как весь список.

<!-- pr-acceptance:machine -->
| Проверка | Джоба | Шаг в `.github/workflows/ci.yml` |
|---|---|---|
| pw-executor: сборка TypeScript + модульные тесты (`npm test` заодно гейтит компиляцию) | `build` | `Build + unit-test pw-executor (TypeScript -> dist/server.js; node:test gates the compile)` |
| Go: `go vet ./...` + `go test ./...` по всему дереву | `build` | `Vet + unit test (Go)` |
| Синтаксис встроенного JS каждой страницы `docs/` (`node --check`, с полом на число страниц) | `build` | `SPA syntax check (inline JS of every docs page — node --check floor gate; M15 + M11.5 PR-4)` |
| DOM-гейт мастера настройки, живой headless Chromium (пол 15) | `build` | `Setup-wizard DOM gate (headless Chromium; M11.5)` |
| DOM-гейт хаба, живой headless Chromium (пол 45) | `build` | `Hub Logs-view DOM gate (headless Chromium; ADR-065)` |
| DOM-гейт статичной витрины — хаб без control-API за спиной (пол 5) | `build` | `Static-showcase DOM gate (the hub with NO control-API behind it; ADR-110)` |
| Питоновский офлайн-сьют: **все** `tests/test_*_offline.py`, обнаруживаются глобом, пол 25 | `build` | `Python offline suite (every tests/test_*_offline.py, discovered — FakeBackend/FakeExecutor, no network)` |
| Сквозной смоук интерфейса против настоящего развёртывания | `build` | `End-to-end UI smoke against a real deployment (screenshots; ADR-110/111)` |
| Выгрузка скриншотов смоука артефактом (`always()` — падение и есть тот случай, когда они нужны) | `build` | `Upload UI smoke screenshots` |
| Детерминированный replay по фикстурам с golden-diff и утверждением кода выхода | `replay` | `Explore + freeze goldens + replay (assert exit code)` |
| Explore по `testdata/site` до `plan.json` | `explore` | `Explore testdata/site -> plan.json` |
| Секреты: gitleaks по **всей истории**, HARD fail | `security` | `gitleaks (secrets scan — HARD fail)` |
| Уязвимости питоновских зависимостей по закреплённому локу (рекомендательно) | `security` | `pip-audit (Python deps — advisory; audits the committed lock)` |
| SBOM CycloneDX из закреплённого лока | `security` | `SBOM (CycloneDX, from the frozen lock — #38)` |
| Двуязычность: у каждого первичного `docs/*.md` есть пара `.en.md` | `bilingual` | `Check bilingual docs parity` |
| Сборка образа `sentinel:local` | `airgap` | `Build sentinel:local (cached, amd64, loaded — not pushed)` |
| Образ знает свою версию | `airgap` | `The image knows its own version` |
| Офлайн-проверка рантайма: `save`/`load` → `--network none`, ни одного внешнего вызова | `airgap` | `Offline runtime verification (save/load -> --network none, no external calls)` |
| Cosign: подпись и **офлайновая** обратная проверка `verify-blob` | `airgap` | `Cosign sign + OFFLINE verify-blob round-trip` |
| `shellcheck` по всем шелл-скриптам | `install-smoke` | `shellcheck all shell scripts` |
| `install.sh` против поддельного релиза + негативный случай с подменённой контрольной суммой | `install-smoke` | `install.sh e2e vs a local fake release (+ tampered-checksum negative)` |
| Сборка четырёх бинарей control-plane для пакета | `deb-smoke` | `Build the four control-plane binaries` |
| Пакет `.deb`: собрать, **установить**, осмотреть, удалить | `deb-smoke` | `Build, install, inspect and remove the package` |
| `collect-live-run.sh`: редактирование по умолчанию, исключения, opt-in `--with-trace` | `collect-live-run-smoke` | `collect-live-run.sh — default redaction + exclusions + --with-trace opt-in` |
| `install.ps1` против поддельного релиза + негативный случай с подменённой суммой | `install-ps1-smoke` | `install.ps1 e2e vs a local fake release (+ tampered-checksum negative)` |
| `golangci-lint` (рекомендательно) | `lint` | `golangci-lint (advisory)` |
| `ruff` по brain и tests (рекомендательно) | `lint` | `ruff (advisory — brain + tests)` |
<!-- /pr-acceptance:machine -->

---

## 2. Ручная половина — это исполняет автор PR

Четыре строки. Каждая несёт **записанную причину**, почему она не в CI: форма скопирована с
`componentsWithoutProbe` (`cmd/control-api/readyz.go`), где отсутствие декларации само по себе
объявлено провалом — иначе у пропуска нет места, куда записать «почему», и гейту пришлось бы
принимать молчание.

Общая причина, из-за которой автоматизация каждой из них была бы **хуже** галочки: галочка «я
смотрел» ложна и опровергаема следующим, кто откроет тот же файл, а шаг CI «PNG существует и
непустой» неопровержим в принципе — он зелёный ровно над тем дефектом, ради которого заводился.
Замер, который это купил: за две сессии автоматические гейты нашли **0 из 9** и **0 из 5** дефектов,
найденных скриншотами и живым прогоном, и оба раза все гейты были зелёными.

<!-- pr-acceptance:manual -->
| Проверка | Как выполняется | Почему не в CI |
|---|---|---|
| **Посмотреть скриншоты** `ui-smoke` — не «шаг зелёный», а открыть кадры и увидеть, что на них | Прогнать смоук (§3) и **открыть PNG**; в теле PR назвать панель и то, что на ней видно | Автоматизировать можно только «файл существует и непустой» — а это гейт, зелёный над тем самым дефектом, ради которого шаг заводится. Замер PR-B: скриншоты нашли шесть кодов события, отрисованных русскому читателю по-английски, и `/readyz` 503, красящий здоровый сервис в error; утверждения ни на то, ни на другое в смоуке не было и не могло быть, потому что никто их не предвидел |
| **Живой прогон на настоящей модели** — не FakeBackend | `LLM_BACKEND=openai LLM_MODEL=qwen3:8b LLM_BASE_URL=<endpoint>/v1 LLM_STRUCTURED=1`, прогон до вердикта; в теле PR назвать модель и результат | Эндпоинт живёт в локальной сети мейнтейнера и с раннера GitHub недостижим. Обе доступные автоматизации хуже галочки: жёстко требовать — красный по причине вне PR, при том что `main` защищён и требует зелёного (это дрессирует мержить поверх красного); пропускать при недоступности — шаг рапортует успех, когда модель не спрашивали. HEALTH-003 уже замерил у этого эндпоинта `HTTP 000` |
| **Докер во всех трёх видах поставки** — `docker-compose.yml`, `.ghcr.yml`, `.offline.yml` | Поднять стек каждым файлом (§3) и проверить затронутую поверхность внутри контейнера | Свежий раннер не воспроизводит то, что этот шаг ловит: root-owned тома против `user: 1000:1000`, ротацию на **живых** контейнерах, переживание журналом `docker compose down`. Дефекты этого класса существуют из-за накопленного состояния ФС хоста; на чистой машине они произойти не могут, и джоба была бы зелёной там, где мерить нечего. В CI сегодня есть только `docker build` одного образа и два `docker run` (джоба `airgap`) — ни одного из трёх compose-файлов |
| **Мутации** — каждая новая проверка обязана уметь падать | Внести правку, ломающую утверждение, убедиться, что тест краснеет, вернуть. Выжившую мутацию либо покрыть, либо записать как эквивалентную рядом с тестом; в теле PR назвать строку и исход | Порог на mutation score теряет ровно то, что мутации дают. Замер: 85 мутаций, 18 выживших, **каждый** — дефект в тесте или фикстуре, ни разу в коде продукта; находка PR-C (удаление `journal(...)` из `cdp-service.ts` оставляет все гейты зелёными) получена потому, что **человек** выбрал эту строку, подозревая непокрытое место вызова. Число вместо суждения удовлетворил бы тот же сьют, который дефект пропустил |
<!-- /pr-acceptance:manual -->

Не каждый PR добавляет компонент. Правка одного дока не обязана поднимать три стека — но пропуск
строки объявляется в теле PR, а не подразумевается: `docs/DEVELOPMENT.md` §0 требует **записанной
причины**, а не молчания.

---

## 3. Как прогнать это локально

Перечни имён здесь **не пишутся** — только команды с глобом. Причина в `docs/DEVELOPMENT.md` §0,
принцип 5: рукописный список не показывает пропущенное, потому что отсутствие не имеет
представления. Этот самый перечень уже протухал в четырёх местах одновременно — 7 имён в
`CONTRIBUTING.md`, 20 в `docs/DEVELOPMENT.md` и `docs/TESTING.md`, 20 в `FILEMAP.md`, — пока в
`tests/` лежали десятки.

```bash
# Go — всё дерево, как в CI
go build ./... && go vet ./... && go test ./...
go test -race ./cmd/control-api/          # там, где меняется разделяемое состояние

# Python — обнаружением, не перечнем
for f in tests/test_*_offline.py; do PYTHONPATH="$PWD" .venv/bin/python "$f"; done

# pw-executor
cd pw-executor && npm test && cd ..

# Три DOM-гейта
node scripts/wizard-dom-check.mjs
node scripts/hub-dom-check.mjs
node scripts/pages-static-check.mjs

# Смоук интерфейса со скриншотами — точный рецепт в ci.yml, шаг «End-to-end UI smoke»
#   ⚠ store-gateway на КОРОТКОМ пути сокета (адрес unix ограничен ~108 байтами),
#     CONTROL_API_SERVE_UI=1 и CONTROL_API_UI_DIR=$PWD/docs — иначе `/` отдаёт 404
node scripts/ui-smoke.mjs --base http://127.0.0.1:8090 --token <token> --out "$PWD/ui-smoke"

# Докер, три вида поставки
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.ghcr.yml up -d
docker compose -f docker-compose.offline.yml --profile demo run --rm demo
```

⚠ **Перед выводом о DOM-гейте припаркуйте `runs/`** (`mv runs/*/ runs/.park/`): каталоги прошлых
прогонов, часть которых root-owned от контейнеров, ломают и `go test ./...`, и счёт гейта.

---

## 4. Записанные исключения

Литеральный перечень имён сьютов (`for t in m3 m4 …`) запрещён гейтом в `CONTRIBUTING.md`,
`.github/PULL_REQUEST_TEMPLATE.md`, `docs/TESTING.md`, `docs/DEVELOPMENT.md`, `FILEMAP.md` и в этом
файле — то есть везде, где он читается как **инструкция к исполнению сегодня**.

`docs/M*_CONTRACT.md` из-под гейта выведены намеренно. Контракт вехи записывает, что было прогнано
для приёмки **той** вехи; переписать его перечень значило бы задним числом изменить запись о
приёмке. Это исключение записано здесь, а не подразумевается списком в коде гейта.
