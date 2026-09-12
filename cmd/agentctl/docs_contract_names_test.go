package main

// [M5-HEAL-VISUAL-FLAG-DOES-NOT-EXIST] — имена, которые ЗАМОРОЖЕННЫЕ контракты вех называют, против
// того, что программа реально принимает.
//
// ЧТО БЫЛО ЗАМЕРЕНО. `docs/M5_CONTRACT.md` описывал поставленный тир тремя именами, которых в
// продукте нет: флаг `--heal-visual`, подкоманда `agentctl heal-poc --scenarios <dir>` и порог
// `completeness_ratio < 0.30`. Соседнее имя из той же фразы (`--heal-llm`) реально, поэтому пара
// читалась как две одинаково существующие ручки: человек пробовал обещанное, получал код 2 и решал,
// что сломана сборка, а не документ. Ни одно из трёх ничем не сверялось — контракт заморожен, а
// продукт менялся под ним.
//
// ⚠ ФЛАГ `--heal-visual` ЗАВЕДЁН (решение Alex, W17): принцип 6 закрывается ПУТЁМ, а не отговоркой.
// Этот гейт — вторая половина того же решения: чтобы следующее расхождение краснело само.
//
// ПОЧЕМУ СПАНЫ ФИЛЬТРУЮТСЯ ПО `agentctl`, А НЕ ПО НАЛИЧИЮ `--`. Первый заход брал любой инлайн-спан
// и собирал 29 «флагов», из которых двенадцать принадлежали чужим программам — `node --check`,
// `uv sync --frozen`, `git tag --list`, `gitleaks detect --source --verbose`, `npx tsc --noEmit` — и
// один был CSS-переменной `var(--x)`. Список исключений на такое был бы длиннее предмета и протухал
// бы вместе с ним. Фильтр по вызову РЕШАЕТ этот класс целиком: спан, не являющийся вызовом agentctl,
// ничего про agentctl и не обещает.
//
// ГДЕ ИСТИНА БЕРЁТСЯ У РАНТАЙМА, А ГДЕ У ИСХОДНИКА, И ПОЧЕМУ. Флаги `run` спрашиваются у НАСТОЯЩЕГО
// `*flag.FlagSet` (newRunFlagSet), потому что у них есть рантайм-форма — тот же довод, что у
// usage_flags_test.go, и та же купленная им ловушка: `--spec` объявлен через `fs.Var`, и грep по
// `fs.String(`/`fs.Bool(` объявил бы его несуществующим. У ПОДКОМАНД рантайм-формы нет: `case "x":`
// спросить не у кого, поэтому они разбираются из исходника — прецедент и оговорка те же, что у
// main_test.go.
//
// ЧТО НАМЕРЕННО НЕ УТВЕРЖДАЕТСЯ: принадлежность флага КОНКРЕТНОЙ подкоманде — кроме `run`, где
// истина рантаймовая и требование точное. Для остальных проверяется существование имени в дереве
// флагов agentctl. Пропуск записан здесь, а не умолчан: сопоставление флага с подкомандой требует
// разбора тела каждой cmdX, и такой разбор ломался бы на каждой перестановке кода — цена выше
// пользы, пока фантомы ловятся существованием.

import (
	"flag"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// contractNamesNotOurs — имена, которые контракты называют законно, хотя продукт их не принимает.
// У каждой записи ОБЯЗАТЕЛЕН текст причины, и запись, ставшая ненужной, роняет гейт: протухшее
// исключение — это тихо выключенная проверка.
var contractNamesNotOurs = map[string]string{
	"remote-debugging-port": "флаг CHROMIUM, а не наш: M9.6_CONTRACT называет им способ, которым ЧЕЛОВЕК " +
		"запускает свой браузер, чтобы Sentinel подключился к уже открытой сессии (`chromium.connectOverCDP`). " +
		"Продукт его не принимает и принимать не должен — обещание здесь адресовано другой программе.",
}

var (
	reSpan     = regexp.MustCompile("`([^`\n]+)`")
	reFlagTok  = regexp.MustCompile(`(?:^|[^\w-])--([a-z][a-z0-9-]*)`)
	reCaseHead = regexp.MustCompile(`case "([a-z][a-z0-9-]*)":`)
	reVerbHead = regexp.MustCompile(`Verb: "([a-z][a-z0-9-]*)`)
	reAnyFlag  = regexp.MustCompile(`fs\.(?:String|Bool|Int|Float64|Duration|Var)\("([a-z][a-z0-9-]*)"`)
	reWord     = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)
	// Спан, состоящий ТОЛЬКО из флагов и разделителей между ними.
	reFlagOnly = regexp.MustCompile(`^--[a-z][a-z0-9-]*([ \t]*[+/,][ \t]*--[a-z][a-z0-9-]*)*$`)
)

func readFile(t *testing.T, path string) string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}
	defer f.Close()
	b, err := io.ReadAll(f)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}

func TestMilestoneContractsNameOnlyThingsTheProgramAccepts(t *testing.T) {
	files, err := filepath.Glob(filepath.Join("..", "..", "docs", "M*_CONTRACT*.md"))
	if err != nil {
		t.Fatal(err)
	}
	// Пол на входное множество: обход, переставший что-либо находить, пройдёт идеально над пустым.
	if len(files) < 40 {
		t.Fatalf("найдено %d контрактов вех — маска перестала совпадать, и всё ниже вакуумно", len(files))
	}

	src := readFile(t, "main.go")
	heads := map[string]bool{}
	for _, m := range reCaseHead.FindAllStringSubmatch(src, -1) {
		heads[m[1]] = true
	}
	for _, m := range reVerbHead.FindAllStringSubmatch(readFile(t, "api.go"), -1) {
		heads[m[1]] = true
	}
	if len(heads) < 12 {
		t.Fatalf("разобрано %d подкоманд — разбор отстал от формы исходника", len(heads))
	}

	// Флаги `run` — у РАНТАЙМА: `--spec` объявлен через `fs.Var`, и грep по конструкторам объявил бы
	// его несуществующим (ловушка, купленная usage_flags_test.go).
	runFlags := map[string]bool{}
	rfs, _ := newRunFlagSet()
	rfs.VisitAll(func(f *flag.Flag) { runFlags[f.Name] = true })
	if len(runFlags) < 18 {
		t.Fatalf("флагсет `run` отдал %d флагов — сравнивать почти не с чем", len(runFlags))
	}
	anyFlag := map[string]bool{}
	for _, m := range reAnyFlag.FindAllStringSubmatch(src, -1) {
		anyFlag[m[1]] = true
	}
	if len(anyFlag) < 20 {
		t.Fatalf("разобрано %d объявлений флагов — разбор отстал от формы исходника", len(anyFlag))
	}

	spans, badHeads, badFlags := 0, []string{}, []string{}
	for _, path := range files {
		for _, m := range reSpan.FindAllStringSubmatch(readFile(t, path), -1) {
			span := strings.TrimSpace(m[1])
			invocation := strings.HasPrefix(span, "agentctl")
			// ⚠ ВТОРАЯ ФОРМА СПАНА, И ОНА КУПЛЕНА МУТАЦИЕЙ. Первая редакция гейта смотрела ТОЛЬКО на
			// вызовы `agentctl …` — и была слепа ровно к тому дефекту, ради которого написана: в
			// M5_CONTRACT фантомный `--heal-visual` упомянут ГОЛЫМ спаном («за `--heal-visual` +
			// `--heal-llm`»), а не вызовом. Мутация «переименовать flag heal-llm» проходила зелёной.
			// Спан из ОДНИХ флагов — это тоже утверждение о нашей программе: чужие вызовы (`node
			// --check`, `uv sync --frozen`, `git tag --list`, `npx tsc --noEmit`) всегда несут имя своей
			// программы первым словом, а `var(--x)` несёт `style=`. Замерено: правило даёт 74 спана и
			// 14 имён, из которых ЧУЖОЕ ровно одно, и оно объявлено исключением.
			if !invocation && !reFlagOnly.MatchString(span) {
				continue
			}
			spans++
			parts := strings.Fields(span)
			if !invocation {
				parts = nil // голый спан подкоманды не называет
			}
			if len(parts) > 1 && reWord.MatchString(parts[1]) && !heads[parts[1]] {
				if _, ok := contractNamesNotOurs[parts[1]]; !ok {
					badHeads = append(badHeads, parts[1]+" ("+filepath.Base(path)+": "+span+")")
				}
			}
			for _, fm := range reFlagTok.FindAllStringSubmatch(span, -1) {
				name := fm[1]
				if runFlags[name] || anyFlag[name] {
					continue
				}
				if _, ok := contractNamesNotOurs[name]; ok {
					continue
				}
				badFlags = append(badFlags, "--"+name+" ("+filepath.Base(path)+": "+span+")")
			}
		}
	}
	if spans < 60 {
		t.Fatalf("в контрактах найдено %d вызовов agentctl — обход сузился, и сравнение слабее, чем читается", spans)
	}

	sort.Strings(badHeads)
	sort.Strings(badFlags)
	if len(badHeads) > 0 {
		t.Errorf("контракт называет подкоманду, которой у программы нет: %s", strings.Join(badHeads, "; "))
	}
	if len(badFlags) > 0 {
		t.Errorf("контракт называет флаг, которого программа не принимает: %s", strings.Join(badFlags, "; "))
	}

	// Протухшее исключение — это тихо выключенная проверка, и оно здесь невозможно по устройству.
	for name := range contractNamesNotOurs {
		if heads[name] || anyFlag[name] {
			t.Errorf("исключение для %q больше не нужно — программа приняла это имя; уберите запись", name)
		}
	}
}
