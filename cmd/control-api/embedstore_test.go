package main

// Гейт встроенного хранилища (ADR-158): развёртывание БЕЗ `CONTROL_API_STORE_ADDR` — тот же продукт,
// а не урезанный, и при этом встроенное хранилище НЕ вытесняет то, у чего своя реализация уже была.
//
// ⚠ ОБЕ ПОЛОВИНЫ ОБЯЗАТЕЛЬНЫ, И ВТОРАЯ КУПЛЕНА ДЕФЕКТОМ. Первая редакция ADR-158 считала встроенное
// хранилище хранилищем везде. Замерено до выпуска: развёртывание, настроенное мастером, на следующем
// старте отвечало `{"error":"no config stored"}` — его `state/config.json` лежал на диске, а пустая
// встроенная база выигрывала. То есть «одна версия» ОТНЯЛА бы у оператора настройки. Поэтому здесь
// проверяется не только «аккаунты появились», но и «файловый ярус ADR-075 на месте»: без второго
// утверждения гейт был бы зелёным над той самой потерей.

import (
	"testing"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
	"net/http"
)

// newTestUser — минимальная учётка. Хеш здесь НЕ настоящий и настоящим быть не должен: эти проверки
// про ХРАНЕНИЕ, а не про проверку пароля (она живёт в internal/identity и покрыта там). Ставить сюда
// реальный хеш значило бы связать гейт хранилища с форматом, который он не проверяет.
func newTestUser(name string) *storepb.User {
	return &storepb.User{UserId: name + "-id", Name: name, PwHash: "not-a-real-hash", IsAdmin: true}
}

func userRefByName(name string) *storepb.UserRef { return &storepb.UserRef{Name: name} }

// TestEmbeddedStoreMakesTheStorelessTierTheSameProduct — восстановленная половина: без внешнего
// шлюза хранилище ЕСТЬ, значит аккаунты и всё, что скоупится владельцем, работают.
//
// Утверждение снимается с ПОВЕДЕНИЯ (`accountsExist` после заведения), а не с наличия поля: `s.store
// != nil` — это повторение формулы реализации, и оно осталось бы зелёным над клиентом, который ни на
// что не отвечает.
func TestEmbeddedStoreMakesTheStorelessTierTheSameProduct(t *testing.T) {
	es, err := startEmbeddedStore(t.TempDir())
	if err != nil {
		t.Fatalf("встроенное хранилище не поднялось: %v", err)
	}
	t.Cleanup(es.stop)
	sc, err := newEmbeddedStoreClient(es)
	if err != nil {
		t.Fatalf("встроенное хранилище не ответило: %v", err)
	}
	t.Cleanup(sc.close)

	s := &server{store: sc, storeEmbedded: true, repo: t.TempDir()}
	if s.accountsExist() {
		t.Fatal("на чистом встроенном хранилище аккаунты уже есть")
	}
	// Заводим аккаунт ЧЕРЕЗ ТОТ ЖЕ клиент, которым ходит шлюзовой путь: если бы встроенный сервер
	// принимал запросы, но не писал, эта проверка бы это увидела — следующий вопрос идёт в базу.
	if !sc.upsertUser(newTestUser("solo")) {
		t.Fatal("встроенное хранилище не приняло аккаунт")
	}
	s.forgetAccounts()
	if !s.accountsExist() {
		t.Fatal("аккаунт заведён, но развёртывание всё ещё считает, что аккаунтов нет")
	}
	if got, ok := sc.getUser(userRefByName("solo")); !ok || got == nil || !got.Found {
		t.Fatalf("аккаунт не читается обратно: ok=%v got=%+v", ok, got)
	}
}

// TestEmbeddedStoreDoesNotDisplaceTheFileConfigTier — ГРАНИЦА, и это регресс, пойманный замером до
// выпуска. Конфиг обязан остаться на файле (ADR-075): у него паритет ярусов был и без хранилища, а
// файл — единственный носитель, который оператор может прочитать и поправить.
func TestEmbeddedStoreDoesNotDisplaceTheFileConfigTier(t *testing.T) {
	es, err := startEmbeddedStore(t.TempDir())
	if err != nil {
		t.Fatalf("встроенное хранилище не поднялось: %v", err)
	}
	t.Cleanup(es.stop)
	sc, err := newEmbeddedStoreClient(es)
	if err != nil {
		t.Fatalf("встроенное хранилище не ответило: %v", err)
	}
	t.Cleanup(sc.close)

	s := &server{store: sc, storeEmbedded: true, repo: t.TempDir()}
	if got := s.configTier(); got != tierFile {
		t.Fatalf("ярус конфига = %q, ожидался %q — встроенное хранилище вытеснило файл ADR-075", got, tierFile)
	}

	// И зеркально: НАСТОЯЩИЙ внешний шлюз обязан по-прежнему давать ярус store. Без этой половины
	// проверка прошла бы над реализацией, которая просто всегда отвечает «файл».
	external := &server{store: sc, storeEmbedded: false, storeAddr: "unix:/nonexistent.sock", repo: t.TempDir()}
	if got := external.configTier(); got != tierStore {
		t.Fatalf("с внешним шлюзом ярус конфига = %q, ожидался %q", got, tierStore)
	}
}

// TestEmbeddedStoreSurvivesARestart — база лежит в state/ и переживает перезапуск процесса. Без этого
// «аккаунты работают» означало бы «работают до первого рестарта», что для аккаунтов не работает вовсе.
func TestEmbeddedStoreSurvivesARestart(t *testing.T) {
	repo := t.TempDir()

	first, err := startEmbeddedStore(repo)
	if err != nil {
		t.Fatalf("первый запуск: %v", err)
	}
	sc1, err := newEmbeddedStoreClient(first)
	if err != nil {
		t.Fatalf("первый клиент: %v", err)
	}
	if !sc1.upsertUser(newTestUser("persisted")) {
		t.Fatal("аккаунт не записался")
	}
	sc1.close()
	first.stop()

	second, err := startEmbeddedStore(repo) // тот же repo -> тот же файл базы
	if err != nil {
		t.Fatalf("второй запуск: %v", err)
	}
	t.Cleanup(second.stop)
	sc2, err := newEmbeddedStoreClient(second)
	if err != nil {
		t.Fatalf("второй клиент: %v", err)
	}
	t.Cleanup(sc2.close)

	got, ok := sc2.getUser(userRefByName("persisted"))
	if !ok || got == nil || !got.Found {
		t.Fatalf("аккаунт не пережил перезапуск: ok=%v got=%+v", ok, got)
	}
}

// TestEveryPersistedSectionReachesARunOnTheFileTier — асимметрия, найденная при разборе ADR-158 и
// существовавшая ДО него.
//
// Секции сохранённого конфига читают три функции. `getPersistedLLM` спрашивала ярус и потому работала
// на обоих; `getPersistedLogging` и `getPersistedSettings` спрашивали `s.store == nil` и на автономном
// ярусе возвращали nil ВСЕГДА. То есть настройки, сохранённые мастером, доезжали до прогона только там,
// где случайно оказался внешний шлюз. Молча: все три сливаются в один слой окружения
// (`mergedPersistedEnv`), а отсутствующий слой неотличим от незаданного значения.
//
// Проверка утверждает НАБЛЮДАЕМОЕ следствие — что в слое есть переменные ИЗ ВСЕХ ТРЁХ секций, — а не
// то, что «функция спрашивает ярус»: последнее было бы повторением формулы реализации.
func TestEveryPersistedSectionReachesARunOnTheFileTier(t *testing.T) {
	repo := t.TempDir()
	s := &server{repo: repo} // ни внешнего шлюза, ни встроенного: чистый файловый ярус
	if got := s.configTier(); got != tierFile {
		t.Fatalf("ярус = %q, ожидался %q", got, tierFile)
	}
	body := `{"llm":{"backend":"openai","base_url":"http://example.invalid/v1","model":"m"},` +
		`"logging":{"level":"debug"},"settings":{}}`
	if err := s.writeConfigFile(body); err != nil {
		t.Fatalf("запись конфига: %v", err)
	}

	env := s.mergedPersistedEnv()
	if len(env) == 0 {
		t.Fatal("сохранённый конфиг не дал ни одной переменной окружения")
	}
	// LLM-секция доезжала и раньше — она здесь как контроль, что стенд вообще рабочий.
	if env["LLM_BACKEND"] != "openai" {
		t.Fatalf("LLM-секция не доехала: %+v", env)
	}
	// А вот это и есть починка: до неё на файловом ярусе ключа не было ни при каких настройках.
	if env["SENTINEL_LOG_LEVEL"] != "debug" {
		t.Fatalf("секция logging не доехала до прогона на файловом ярусе: %+v", env)
	}
}

// TestSavedPersonalSettingsReachARunOnTheStandaloneTier — ЯРУС, НА КОТОРОМ ЭТОГО НЕ ПРОВЕРЯЛ НИКТО,
// и именно поэтому там всё было сломано.
//
// ЗАМЕРЕННЫЙ ДЕФЕКТ. Личные умолчания прогона (W15) читались прямо из `s.store`, а на автономном
// ярусе `configTier()` отвечает `tierFile` — PUT клал документ в `state/config.json`, читатель
// смотрел во встроенную базу, куда конфиг не писался НИКОГДА. Человек проходил мастер, сохранял
// `run`/`auth`, получал «✓ сохранено … действует на следующие прогоны», видел значения обратно в
// форме — подтверждение приходило ДВАЖДЫ, — и ни один прогон их не наследовал.
//
// Дефект был невидим по одной причине: соседний тест `TestSavedPersonalSettingsActuallyReachARun`
// поднимается через `storeBackedServer`, то есть ВСЕГДА на ярусе с внешним шлюзом. Ярус, которым
// пользуется одиночное развёртывание, не покрывал ни один тест — а действующая директива говорит,
// что ярус это развёртывание, а не другой продукт.
//
// KILLS: возврат `applyPersonalRunDefaults` к прямому `s.store.getConfig`; запись личных секций в
// общий файл; ответ «сохранено» без состава записанного.
func TestSavedPersonalSettingsReachARunOnTheStandaloneTier(t *testing.T) {
	es, err := startEmbeddedStore(t.TempDir())
	if err != nil {
		t.Fatalf("встроенное хранилище не поднялось: %v", err)
	}
	t.Cleanup(es.stop)
	sc, err := newEmbeddedStoreClient(es)
	if err != nil {
		t.Fatalf("встроенное хранилище не ответило: %v", err)
	}
	t.Cleanup(sc.close)

	s := newTestServer()
	s.store, s.storeEmbedded = sc, true
	if s.configTier() != tierFile {
		t.Fatalf("ярус %q — тест мерит не то развёртывание", s.configTier())
	}

	// Аккаунт заводится так же, как это делает развёртывание при первом старте (ADR-159).
	if !s.store.upsertUser(&storepb.User{UserId: "u-solo", Name: "solo", PwHash: "x", IsAdmin: false}) {
		t.Fatal("аккаунт не завёлся во встроенном хранилище")
	}
	tok := s.sessions.mint("u-solo", "solo", false, sessionTTL())

	// 1. ЧЕЛОВЕК СОХРАНЯЕТ СВОЁ. Ответ обязан НАЗВАТЬ состав записанного: «сохранено» над пустым
	// документом — это подтверждение несделанного, и оно хуже отказа.
	rec, body := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"run":{"max_steps":5,"target":"http://solo.example/"}}`), tok)
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT личной секции на автономном ярусе = %d (%s)", rec.Code, rec.Body.String())
	}
	written, _ := body["written"].(map[string]any)
	if written == nil || written["personal"] == nil {
		t.Errorf("ответ не назвал, что записана личная секция: %v", body)
	}

	// 2. И ЧИТАЕТ ЕЁ ОБРАТНО КАК СВОЮ — с той же формой ответа, что на ярусе со шлюзом.
	rec2, got := doJSON(t, s, http.MethodGet, "/v1/config", nil, tok)
	if rec2.Code != http.StatusOK {
		t.Fatalf("GET = %d (%s)", rec2.Code, rec2.Body.String())
	}
	sources, _ := got["sources"].(map[string]any)
	if sources["run"] != "user" {
		t.Errorf("секция run числится не за человеком: sources=%v — форма ответа отличается от яруса со шлюзом", sources)
	}
	if _, ok := got["may_write_global"]; !ok {
		t.Error("ответ автономного яруса не несёт may_write_global — интерфейс не может узнать, что человеку нельзя менять")
	}

	// 3. ГЛАВНОЕ: СОХРАНЁННОЕ ДОЕЗЖАЕТ ДО ПРОГОНА. Круговорот документа этого не доказывает —
	// он и был исправен, пока значение не действовало ни на что.
	req := runRequest{owner: "u-solo"}
	used := s.applyPersonalRunDefaults(&req)
	if req.MaxSteps != "5" || req.Target != "http://solo.example/" {
		t.Fatalf("на автономном ярусе умолчания не доехали до прогона: %+v (применено %v)", req, used)
	}
	if len(used) != 2 {
		t.Errorf("применено %v — ответ обязан назвать унаследованное поимённо", used)
	}

	// 4. И ЯВНЫЙ ВЫБОР ПО-ПРЕЖНЕМУ ПОБЕЖДАЕТ — на этом ярусе тоже.
	req2 := runRequest{owner: "u-solo", MaxSteps: "1"}
	s.applyPersonalRunDefaults(&req2)
	if req2.MaxSteps != "1" {
		t.Errorf("умолчание перебило явный выбор: %q", req2.MaxSteps)
	}
}
