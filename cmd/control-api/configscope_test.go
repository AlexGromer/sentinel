package main

// Gates for the config split (configscope.go) — Alex's directive extending ADR-109: the tool belongs
// to the master user, everything the working person configures belongs to them.
//
// The property being asserted is NOT "these five sections are classified". A gate that listed them
// would agree with a sixth section that nobody classified — which is exactly the failure mode the
// fail-closed refusal exists to prevent. So the gates ask instead: can an unclassified section reach
// the store at all, can a non-admin change the tool, and does one account's document ever touch
// another's.

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// storeBackedServer is a control-API with a real gateway and two accounts, one admin.
func storeBackedServer(t *testing.T) (*server, string, string, string) {
	t.Helper()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sc.close)
	s := newTestServer()
	s.store, s.storeAddr = sc, addr
	sc.upsertUser(&storepb.User{UserId: "ua", Name: "alice", PwHash: "x", IsAdmin: true})
	sc.upsertUser(&storepb.User{UserId: "ub", Name: "bob", PwHash: "x"})
	sc.upsertUser(&storepb.User{UserId: "uc", Name: "carol", PwHash: "x"})
	s.forgetAccounts()
	return s, s.sessions.mint("ua", "alice", true, sessionTTL()),
		s.sessions.mint("ub", "bob", false, sessionTTL()),
		s.sessions.mint("uc", "carol", false, sessionTTL())
}

func getConfigAs(t *testing.T, s *server, token string) (int, map[string]any) {
	t.Helper()
	rec, body := doJSON(t, s, http.MethodGet, "/v1/config", nil, token)
	return rec.Code, body
}

// TestUnclassifiedSectionIsRefusedAndNothingIsWritten is the fail-closed rule, and the second half
// matters as much as the first: a refusal that had already written the sections it recognised would
// leave a half-applied configuration behind an error message.
func TestUnclassifiedSectionIsRefusedAndNothingIsWritten(t *testing.T) {
	s, admin, _, _ := storeBackedServer(t)

	rec, body := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"telepathy":{"enabled":true}}`), admin)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("PUT with an unclassified section = %d, want 400 (%s)", rec.Code, rec.Body.String())
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "telepathy") {
		t.Errorf("the refusal does not name the offending section: %q", msg)
	}
	if code, _ := getConfigAs(t, s, admin); code != http.StatusNotFound {
		t.Errorf("a refused PUT still wrote something: GET /v1/config = %d, want 404", code)
	}
}

// TestNonAdminCannotChangeTheTool: the whole point of the split. And it is refused BY NAME — silently
// dropping the tool's sections would answer "saved" to someone whose change never happened.
func TestNonAdminCannotChangeTheTool(t *testing.T) {
	s, admin, bob, _ := storeBackedServer(t)

	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"settings":{"log_keep":5}}`), admin); rec.Code != http.StatusOK {
		t.Fatalf("admin PUT of the tool's config = %d", rec.Code)
	}

	rec, body := doJSON(t, s, http.MethodPut, "/v1/config", []byte(`{"settings":{"log_keep":999}}`), bob)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("non-admin writing a global section = %d, want 403 (%s)", rec.Code, rec.Body.String())
	}
	if msg, _ := body["error"].(string); !strings.Contains(msg, "settings") {
		t.Errorf("the refusal does not name the refused section: %q", msg)
	}
	// The document is the evidence, not the status code: a 403 that had written anyway would look
	// identical from here.
	_, got := getConfigAs(t, s, admin)
	cfg, _ := got["config"].(map[string]any)
	settings, _ := cfg["settings"].(map[string]any)
	if settings["log_keep"] != float64(5) {
		t.Errorf("a refused write changed the tool's configuration: log_keep = %v, want 5", settings["log_keep"])
	}
}

// TestPersonalSectionsAreEachAccountsOwn: bob's run settings are bob's, carol's are carol's, and
// neither is the deployment's.
func TestPersonalSectionsAreEachAccountsOwn(t *testing.T) {
	s, admin, bob, carol := storeBackedServer(t)

	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"run":{"max_steps":40}}`), admin); rec.Code != http.StatusOK {
		t.Fatalf("admin seeding config: %d", rec.Code)
	}
	for tok, steps := range map[string]int{bob: 10, carol: 99} {
		if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
			[]byte(`{"run":{"max_steps":`+strconv.Itoa(steps)+`}}`), tok); rec.Code != http.StatusOK {
			t.Fatalf("personal PUT (%d steps) = %d", steps, rec.Code)
		}
	}
	for tok, want := range map[string]float64{bob: 10, carol: 99} {
		_, got := getConfigAs(t, s, tok)
		cfg, _ := got["config"].(map[string]any)
		run, _ := cfg["run"].(map[string]any)
		if run["max_steps"] != want {
			t.Errorf("account read max_steps=%v, want %v — the personal layers are sharing a document", run["max_steps"], want)
		}
		// The tool's section still reaches them: a personal document layers OVER the global one, it does
		// not replace it. Without this a person who saved one preference would lose the deployment's LLM.
		if _, ok := cfg["llm"]; !ok {
			t.Error("the global llm section vanished for an account with a personal document")
		}
		// And `sources` says which layer each section came from — not derivable from the merged document,
		// and what an interface needs to show what a reset would restore.
		sources, _ := got["sources"].(map[string]any)
		if sources["run"] != "user" || sources["llm"] != "global" {
			t.Errorf("sources = %v, want run:user llm:global", sources)
		}
	}
	// The admin's own view is unaffected by either of them.
	_, adminGot := getConfigAs(t, s, admin)
	acfg, _ := adminGot["config"].(map[string]any)
	arun, _ := acfg["run"].(map[string]any)
	if arun["max_steps"] != float64(40) {
		t.Errorf("another account's personal document leaked into the global one: %v", arun["max_steps"])
	}
}

// TestMachineTokenWritesTheGlobalDocument pins the rule that keeps the setup wizard working unchanged:
// a caller with no subject writes the tool's document for BOTH halves. Before the split the wizard
// saved run/auth defaults for the deployment; it authenticates as the machine, and it still does.
func TestMachineTokenWritesTheGlobalDocument(t *testing.T) {
	s, admin, bob, _ := storeBackedServer(t)

	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"backend":"anthropic"},"run":{"max_steps":40},"auth":{"storage_state":"/s.json"}}`),
		s.token); rec.Code != http.StatusOK {
		t.Fatalf("machine PUT = %d", rec.Code)
	}
	// An account that has saved nothing of its own sees the deployment's defaults, run/auth included.
	for _, tok := range []string{admin, bob} {
		_, got := getConfigAs(t, s, tok)
		cfg, _ := got["config"].(map[string]any)
		run, _ := cfg["run"].(map[string]any)
		if run["max_steps"] != float64(40) {
			t.Errorf("the wizard's run defaults did not reach an account: %v", run["max_steps"])
		}
		sources, _ := got["sources"].(map[string]any)
		if sources["run"] != "global" {
			t.Errorf("sources[run] = %v, want global — nobody saved a personal one", sources["run"])
		}
	}
}

// TestMayWriteGlobalTravelsWithTheDocument: the interface has to disable what a caller cannot change,
// and it cannot infer that from the document.
func TestMayWriteGlobalTravelsWithTheDocument(t *testing.T) {
	s, admin, bob, _ := storeBackedServer(t)
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(`{"llm":{"backend":"anthropic"}}`), admin); rec.Code != http.StatusOK {
		t.Fatal(rec.Code)
	}
	for tok, want := range map[string]bool{admin: true, bob: false, s.token: true} {
		_, got := getConfigAs(t, s, tok)
		if got["may_write_global"] != want {
			t.Errorf("may_write_global = %v, want %v", got["may_write_global"], want)
		}
	}
}

// TestSchemaPublishesTheClassification: the map that ENFORCES the split is the map the UI reads, so a
// section cannot be admin-only in one and personal in the other.
func TestSchemaPublishesTheClassification(t *testing.T) {
	rec, body := doJSON(t, newTestServer(), http.MethodGet, "/v1/config-schema", nil, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("config-schema = %d", rec.Code)
	}
	published, _ := body["config_sections"].(map[string]any)
	if len(published) != len(configSectionScope) {
		t.Fatalf("schema publishes %d sections, the enforcer knows %d", len(published), len(configSectionScope))
	}
	for name, scope := range configSectionScope {
		if published[name] != string(scope) {
			t.Errorf("section %q: schema says %v, the enforcer says %q", name, published[name], scope)
		}
	}
}

// TestTheWizardOnlyWritesClassifiedSections reads the section names out of the setup wizard's own
// document builder. The wizard is the main writer of this document and lives in another language, so
// a section added there without a classification would not fail until a person pressed save and got a
// 400 — in production, on their configuration, with no CI signal at all.
func TestTheWizardOnlyWritesClassifiedSections(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "docs", "setup", "index.html"))
	if err != nil {
		t.Fatal(err)
	}
	body := regexp.MustCompile(`(?s)function buildConfigDoc\s*\(\)\s*\{.*?\n\}`).Find(raw)
	if body == nil {
		t.Fatal("buildConfigDoc not found in docs/setup/index.html — this gate would pass by reading nothing")
	}
	// ⚠ РАЗБОР ПЕРЕПИСАН В W16, И ВОТ ПРИЧИНА. Раньше он выхватывал ОДИН `return { … };` литерал —
	// то есть предполагал, что состав документа известен на момент возврата и постоянен. В W16
	// состав стал условным: глобальные секции кладутся только тому, кому их можно менять (иначе
	// сервер отказывает всему документу, и обычный аккаунт не мог сохранить даже своё), а `settings`
	// — только когда в ней что-то есть. Литерала больше нет, и старый разбор падал ГРОМКО, как и
	// задуман. Теперь имена собираются из ОБЕИХ форм записи секции, потому что обе реальны:
	// член объектного литерала (`run: run,`) и присваивание члену (`doc.llm = llm;`).
	names := map[string]bool{}
	for _, m := range regexp.MustCompile(`(?m)^\s{4}(\w+):\s`).FindAllSubmatch(body, -1) {
		names[string(m[1])] = true
	}
	for _, m := range regexp.MustCompile(`\bdoc\.(\w+)\s*=`).FindAllSubmatch(body, -1) {
		names[string(m[1])] = true
	}
	// Пол: разбор, переставший что-либо находить, проходит идеально над пустым множеством — и это
	// единственное, чего сам вывод не ловит (docs/DEVELOPMENT.md §0, принцип 5). Замерено 4:
	// run, auth, llm, settings.
	if len(names) < 4 {
		t.Fatalf("разбор нашёл %d секций (%v) при поле 4 — он сломался, и проверка ниже стала бы "+
			"вакуумной:\n%s", len(names), names, body)
	}
	for name := range names {
		if _, ok := configSectionScope[name]; !ok {
			t.Errorf("the setup wizard writes a %q section that configscope.go does not classify — saving "+
				"from the wizard would 400 in production", name)
		}
	}
	// Обратная сторона, и она новая: мастер рисует контролы настроек из схемы, а собирал документ
	// без них — 44 операторские ручки принимались формой и не сохранялись никогда. Требуем, чтобы
	// секция `settings` вообще участвовала в сборке.
	if !names["settings"] {
		t.Error("buildConfigDoc не собирает секцию `settings`, хотя мастер рисует её контролы из схемы: " +
			"человек заполняет форму, получает «✓ конфиг сохранён» и не сохраняет из неё ничего")
	}
}

// TestSplitPreservesTheCallersBytes: the stored document must be the document that was sent. Round
// tripping through map[string]any would rewrite numbers (40 becoming 4e+01) and reorder members, so
// "save my configuration" would quietly save something merely equivalent to it.
func TestSplitPreservesTheCallersBytes(t *testing.T) {
	s, admin, _, _ := storeBackedServer(t)
	const doc = `{"settings":{"log_keep":40,"heal_auto":0.85},"run":{"max_steps":40}}`
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(doc), admin); rec.Code != http.StatusOK {
		t.Fatalf("PUT = %d", rec.Code)
	}
	rec, err := s.store.getConfig(setupConfigKey, "", storeCallTimeout)
	if err != nil || rec == nil {
		t.Fatalf("reading back: %v", err)
	}
	if !strings.Contains(rec.ValueJson, `"log_keep":40`) || !strings.Contains(rec.ValueJson, `"heal_auto":0.85`) {
		t.Errorf("the stored bytes were rewritten: %s", rec.ValueJson)
	}

	// The BYTES, not merely equivalent JSON — this is what the first version of this check got wrong,
	// and a mutation that round-tripped the document through map[string]any survived it. Two things a
	// round trip destroys and substring matching cannot see:
	//
	//	member ORDER inside a section — Go sorts map keys, so a document comes back rearranged and
	//	  every diff an operator takes of their own config shows changes nobody made;
	//	integer PRECISION — a JSON number becomes float64, so 2^53+1 silently returns as 2^53. A
	//	  budget or a timeout is exactly the kind of value that lives here.
	const exact = `{"settings":{"zzz_last":1,"aaa_first":2,"big":9007199254740993}}`
	if rec2, _ := doJSON(t, s, http.MethodPut, "/v1/config", []byte(exact), admin); rec2.Code != http.StatusOK {
		t.Fatalf("PUT of the byte-exactness document = %d", rec2.Code)
	}
	stored, err := s.store.getConfig(setupConfigKey, "", storeCallTimeout)
	if err != nil || stored == nil {
		t.Fatalf("reading back: %v", err)
	}
	if stored.ValueJson != exact {
		t.Errorf("the document was re-serialised on the way in:\n sent   %s\n stored %s", exact, stored.ValueJson)
	}
	// Sections are stored in a stable order, so an operator diffing the stored document sees changes,
	// not member shuffling.
	var first, second map[string]json.RawMessage
	_ = json.Unmarshal([]byte(rec.ValueJson), &first)
	again, _ := marshalConfigDoc(first)
	_ = json.Unmarshal(again, &second)
	if reMarshalled, _ := marshalConfigDoc(second); string(reMarshalled) != string(again) {
		t.Error("re-serialising the same document produced different bytes")
	}
}

// TestSavedPersonalSettingsActuallyReachARun — ЗАМЕРЕННЫЙ ДЕФЕКТ: секции `run` и `auth` объявлялись
// настраиваемыми, мастер их сохранял, `GET /v1/config` их отдавал — и НИКТО их не читал. В окружение
// прогона материализуются только глобальные секции, а личные, по замыслу, были «умолчаниями формы,
// которые заполняет интерфейс»; интерфейс их не заполнял: обращений к `cfgDoc.run` в хабе не было ни
// одного. Человек сохранял настройку, получал подтверждение — и не действовало ничего.
//
// Утверждаются ТРИ свойства, и второе не менее важно первого: умолчание не спорит с выбором.
func TestSavedPersonalSettingsActuallyReachARun(t *testing.T) {
	s, _, bob, carol := storeBackedServer(t)
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"run":{"max_steps":7,"target":"http://saved.example/","planner":"llm"}}`), bob); rec.Code != http.StatusOK {
		t.Fatalf("сохранение личной секции: %d", rec.Code)
	}

	// 1. ПУСТОЕ ПОЛЕ ЗАПОЛНЯЕТСЯ СОХРАНЁННЫМ — и заполненное НАЗЫВАЕТСЯ.
	req := runRequest{owner: ownerOfToken(t, s, bob)}
	used := s.applyPersonalRunDefaults(&req)
	if req.MaxSteps != "7" || req.Target != "http://saved.example/" || req.Planner != "llm" {
		t.Errorf("сохранённые умолчания не доехали до запроса: %+v", req)
	}
	if len(used) != 3 {
		t.Errorf("применено %v — ответ обязан НАЗВАТЬ унаследованное поимённо, иначе прогон уходит по адресу, "+
			"которого человек в этом запросе не видел", used)
	}
	// Число 7 приходит из JSON как float64, и «7» здесь — не косметика: «7.000000» доехало бы до
	// agentctl мусором, и прогон упал бы по причине, к настройке не относящейся.
	if req.MaxSteps != "7" {
		t.Errorf("число приведено к строке неверно: %q", req.MaxSteps)
	}

	// 2. ЯВНЫЙ ВЫБОР ПОБЕЖДАЕТ. Без этого «я не выбирал» и «я выбрал ровно это» стали бы одним актом —
	// ровно то, что запрещает довод у runRequest.Observe.
	req2 := runRequest{owner: req.owner, MaxSteps: "3", Target: "http://asked.example/"}
	used2 := s.applyPersonalRunDefaults(&req2)
	if req2.MaxSteps != "3" || req2.Target != "http://asked.example/" {
		t.Errorf("умолчание перебило явный выбор: %+v", req2)
	}
	for _, u := range used2 {
		if u == "run.max_steps" || u == "run.target" {
			t.Errorf("в списке унаследованного оказалось поле, которое человек задал сам: %v", used2)
		}
	}

	// 3. ЧУЖОЕ УМОЛЧАНИЕ НЕ ПРИМЕНЯЕТСЯ. Личный слой на то и личный; иначе настройка одного человека
	// молча меняла бы прогоны другого — дефект строго хуже того, который здесь чинится.
	req3 := runRequest{owner: ownerOfToken(t, s, carol)}
	if used3 := s.applyPersonalRunDefaults(&req3); len(used3) != 0 || req3.MaxSteps != "" {
		t.Errorf("аккаунту достались чужие умолчания: %v %+v", used3, req3)
	}

	// 4. БЕЗ ВЛАДЕЛЬЦА — БЕЗ УМОЛЧАНИЙ. Машинный кредентиал и развёртывание без аккаунтов имеют
	// владельца "", и «нет субъекта — нет скоупинга» здесь действует так же, как везде в ADR-109.
	req4 := runRequest{}
	if used4 := s.applyPersonalRunDefaults(&req4); len(used4) != 0 {
		t.Errorf("безвладельческому прогону достались чьи-то умолчания: %v", used4)
	}
}

// ownerOfToken — id аккаунта по его сессии; тесту он нужен затем же, зачем обработчику: умолчания
// адресуются владельцу, а не токену.
func ownerOfToken(t *testing.T, s *server, tok string) string {
	t.Helper()
	c, ok := s.resolveCred(tok)
	if !ok {
		t.Fatalf("кредентиал не разобрался")
	}
	return c.owner()
}

// TestEveryDoorThatSpawnsARunAppliesPersonalDefaults — ЧЕТВЁРТАЯ ДВЕРЬ, и найдена она была ПОСЛЕ
// того, как ADR-166 назвал её поимённо в собственном обосновании.
//
// Довод ADR-166 звучит так: читатель личных умолчаний живёт на СЕРВЕРЕ, потому что тело прогона шлют
// четыре разных места, и читатель в интерфейсе чинил бы одну дверь из четырёх. Замерено: сам
// читатель был подключён к ОДНОЙ двери — `handleCreateRun`. Заглушка `/v1/chat/completions`
// собирала `runRequest` в процессе и звала `spawnRun` напрямую, то есть человек, работающий через
// OpenAI-совместимый эндпоинт, получал прогон без своих настроек и не узнавал об этом.
//
// ⚠ ПЕРЕЧЕНЬ ДВЕРЕЙ ВЫВОДИТСЯ ИЗ КОДА, А НЕ ПИШЕТСЯ. Список, написанный руками, был бы написан тем
// же пониманием, что породило дыру, и пятая дверь появилась бы в нём молча. Здесь ищутся ВСЕ места,
// зовущие `spawnRun`, и каждое обязано либо звать `applyPersonalRunDefaults`, либо нести рядом
// записанную причину.
func TestEveryDoorThatSpawnsARunAppliesPersonalDefaults(t *testing.T) {
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatalf("main.go: %v", err)
	}
	lines := strings.Split(string(src), "\n")
	var doors []int
	for i, ln := range lines {
		if strings.Contains(ln, "s.spawnRun(") && !strings.Contains(ln, "func ") {
			doors = append(doors, i)
		}
	}
	if len(doors) < 3 {
		t.Fatalf("найдено %d дверей — разбор сломался, и утверждение стало бы вакуумным", len(doors))
	}
	for _, i := range doors {
		// ⚠ ИЩЕМ В ТЕЛЕ ФУНКЦИИ, А НЕ В ОКНЕ ФИКСИРОВАННОЙ ВЫСОТЫ. Первая редакция брала двадцать
		// строк выше и обвинила `handleCreateRun`, где вызов стоит сразу после разбора тела — то есть
		// сотней строк раньше. Окно отвечает на вопрос «рядом ли», а спросить надо «в этой ли
		// функции»: дверь — это функция, а не соседство строк.
		// ⚠ ГРАНИЦА — БЛИЖАЙШИЙ ПРЕДЫДУЩИЙ СПАВН ИЛИ НАЧАЛО ФУНКЦИИ, что ближе. Вторая редакция брала
		// всё тело функции и была ВАКУУМНОЙ: у заглушки чата два спавна в одной функции, и вызов из
		// первой ветки закрывал вторую — мутация «убрать вызов у второй двери» ВЫЖИЛА. Каждая дверь
		// обязана отвечать за себя; без этой границы гейт утверждал «в функции где-то есть вызов»,
		// а спросить надо «есть ли он у ЭТОГО спавна».
		lo := 0
		for k := i - 1; k >= 0; k-- {
			if strings.Contains(lines[k], "s.spawnRun(") || strings.HasPrefix(lines[k], "func ") {
				lo = k + 1
				break
			}
		}
		window := strings.Join(lines[lo:i], "\n")
		if strings.Contains(window, "applyPersonalRunDefaults") {
			continue
		}
		if strings.Contains(window, "БЕЗ ЛИЧНЫХ УМОЛЧАНИЙ:") {
			continue // записанная причина, а не молчание
		}
		t.Errorf("main.go:%d зовёт spawnRun, не применив личные умолчания и не назвав причину:\n    %s\n"+
			"Человек, пришедший этой дверью, получит прогон без своих сохранённых настроек и не узнает об этом.",
			i+1, strings.TrimSpace(lines[i]))
	}
}

// TestReplayDoesNotInheritTheSavedTarget — РЕГРЕССИЯ, которую завёл сам ADR-166, и она высшей цены.
//
// Кнопка «🔁 Перепрогон» шлёт тело БЕЗ адреса: адрес берётся из замороженного плана. Личные
// умолчания применяются ДО разбора режима, поэтому подставленный `run.target` занимал пустое место,
// ветка replay видела валидный адрес, и откат на `plan.target_url` не срабатывал НИКОГДА — план
// проигрывался против СОХРАНЁННОГО адреса, а кнопка обещала повторить ТОТ ЖЕ прогон. Комментарий
// «request target wins» при этом оставался верным буквально и ложным по смыслу: «запрошенным»
// оказывалось то, чего человек не вводил.
//
// KILLS: снятие `planDerivedModes` с поля `target`; возврат применения умолчаний в режимах,
// выводящих поля из плана.
func TestReplayDoesNotInheritTheSavedTarget(t *testing.T) {
	s, _, bob, _ := storeBackedServer(t)
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"run":{"target":"http://saved.example/","max_steps":9},"auth":{"login_plan":"runs/login/plan.json"}}`), bob); rec.Code != http.StatusOK {
		t.Fatalf("сохранение личной секции: %d", rec.Code)
	}
	owner := ownerOfToken(t, s, bob)

	for _, mode := range []string{"replay", "baseline"} {
		req := runRequest{owner: owner, Mode: mode, FromRun: "prior"}
		used := s.applyPersonalRunDefaults(&req)
		if req.Target != "" {
			t.Errorf("режим %q унаследовал сохранённый адрес %q — замороженный план пойдёт не туда, "+
				"а кнопка обещала повторить ТОТ ЖЕ прогон", mode, req.Target)
		}
		if req.LoginPlan != "" {
			t.Errorf("режим %q унаследовал план входа %q — он доезжает той же переменной, что план "+
				"воспроизведения, и заведомо не применится; ответ объявил бы применённым несделанное", mode, req.LoginPlan)
		}
		for _, u := range used {
			if u == "run.target" || u == "auth.login_plan" {
				t.Errorf("режим %q объявил унаследованным поле, которое выводит сам: %v", mode, used)
			}
		}
		// А то, что режим НЕ выводит, наследоваться обязано — иначе правка вылечила бы дефект,
		// отняв возможность.
		if req.MaxSteps != "9" {
			t.Errorf("режим %q потерял умолчание, к плану отношения не имеющее: max_steps=%q", mode, req.MaxSteps)
		}
	}

	// И в обычном режиме адрес по-прежнему наследуется: исключение точечное, а не запрет.
	req := runRequest{owner: owner, Mode: "explore"}
	s.applyPersonalRunDefaults(&req)
	if req.Target != "http://saved.example/" {
		t.Errorf("explore перестал наследовать адрес: %q — исключение расползлось за свои режимы", req.Target)
	}
}
