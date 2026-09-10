package main

// Частичный PUT перестал уносить чужие секции — и это НЕ расхождение ярусов, вопреки записи реестра.
//
// ЧТО БЫЛО ЗАМЕРЕНО, И ЧТО ИЗ ЗАПИСИ НЕ ПОДТВЕРДИЛОСЬ. Реестр утверждал: «на ярусе store запрос
// пишет ТОЛЬКО личный слой, а на ярусе file тот же запрос перезаписывает ВЕСЬ файл — секции llm,
// settings, logging развёртывания исчезают», и приводил в пример тело `{"run":{…}}`. С ADR-168 это
// неправда: `run` и `auth` — секции ЛИЧНЫЕ (configscope.go), поэтому глобальная половина такого
// запроса ПУСТА, файл не трогается вовсе, и оба яруса ведут себя одинаково. Пример записи не
// воспроизводится.
//
// Дефект, который воспроизводится, — другой и общий для ОБОИХ ярусов: PUT, несущий ПОДМНОЖЕСТВО
// глобальных секций, заменяет глобальный документ целиком, и секции, которых в запросе не было,
// исчезают молча. Ответ при этом говорит «saved» и перечисляет записанное — то есть подтверждает
// ровно то, что человек прислал, и молчит о том, что унёс.
//
// ПОЧЕМУ ЭТО ДОРОГО ИМЕННО СЕЙЧАС. Мастер настройки шлёт `{llm, run, auth}` и НЕ шлёт `settings`
// (docs/setup/index.html: `buildConfigDoc` не читает контролы, которые сам же нарисовал, — их
// читает соседний `buildEnv`). Значит сохранение из мастера СТИРАЕТ ранее сохранённый блок
// `settings`: 44 операторские ручки, включая политику падения прогона и ретенцию артефактов.
// Два дефекта складываются в один сценарий, и починка каждого по отдельности его не закрывает.
//
// ПОЧЕМУ УСЕЧЕНИЕ ОСТАЁТСЯ ОТДЕЛЬНЫМ УТВЕРЖДЕНИЕМ. `TestConfigFileResaveReplacesTheDocument`
// закрепляет свойство файлового БЭКЕНДА: короткий документ, записанный поверх длинного, не должен
// оставлять хвост предыдущего. Это верно и после слияния — слияние меняет, ЧТО пишется, а не то,
// как пишется. Поэтому тот тест переписан на два раздельных утверждения, а не удалён (правило:
// гейт, покрасневший на осознанной правке, переписывается с записанной причиной).

import (
	"encoding/json"
	"net/http"
	"testing"
)

// putSections PUTs a document and returns the response body.
func putSections(t *testing.T, s *server, doc string) map[string]any {
	t.Helper()
	rec, body := doJSON(t, s, http.MethodPut, "/v1/config", []byte(doc), "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT %s = %d (%s)", doc, rec.Code, rec.Body.String())
	}
	return body
}

func configNow(t *testing.T, s *server) map[string]any {
	t.Helper()
	rec, body := doJSON(t, s, http.MethodGet, "/v1/config", nil, "secret-tok")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /v1/config = %d (%s)", rec.Code, rec.Body.String())
	}
	cfg, ok := body["config"].(map[string]any)
	if !ok {
		t.Fatalf("GET returned no config document: %v", body)
	}
	return cfg
}

// TestAPartialSaveKeepsTheSectionsItDidNotCarry — это и есть сценарий мастера, разложенный на два
// запроса: сперва оператор сохранил настройки развёртывания, потом кто-то сохранил подключение к
// модели. Второе не имеет права стереть первое.
func TestAPartialSaveKeepsTheSectionsItDidNotCarry(t *testing.T) {
	s := fileTierServer(t)

	putSections(t, s, `{"settings":{"artifact_retention_days":7},"logging":{"level":"debug"}}`)
	putSections(t, s, `{"llm":{"backend":"openai","base_url":"http://ollama.lan:11434/v1"}}`)

	cfg := configNow(t, s)
	st, ok := cfg["settings"].(map[string]any)
	if !ok {
		t.Fatalf("сохранение `llm` унесло весь блок `settings` — 44 операторские ручки, о которых "+
			"ответ ничего не сказал. Это и есть сценарий мастера: он шлёт llm и не шлёт settings.\n"+
			"документ сейчас: %v", cfg)
	}
	if got := st["artifact_retention_days"]; got != float64(7) {
		t.Errorf("`settings.artifact_retention_days` = %v, было 7", got)
	}
	if lg, ok := cfg["logging"].(map[string]any); !ok || lg["level"] != "debug" {
		t.Errorf("`logging` не пережил чужое сохранение: %v", cfg["logging"])
	}
	if llm, ok := cfg["llm"].(map[string]any); !ok || llm["backend"] != "openai" {
		t.Errorf("сохранённое в этом запросе не записалось: %v", cfg["llm"])
	}
}

// Слияние не имеет права стать «нельзя ничего убрать»: явный null снимает секцию. Без этого
// единственным способом убрать настройку остаётся правка файла руками, а ручка «сбросить» в
// интерфейсе стала бы неисполнимой.
func TestAnExplicitNullRemovesASection(t *testing.T) {
	s := fileTierServer(t)

	putSections(t, s, `{"settings":{"artifact_retention_days":7},"logging":{"level":"debug"}}`)
	putSections(t, s, `{"logging":null}`)

	cfg := configNow(t, s)
	if _, still := cfg["logging"]; still {
		t.Errorf("явный null не убрал секцию — убрать настройку стало нечем: %v", cfg)
	}
	if _, gone := cfg["settings"]; !gone {
		t.Errorf("удаление одной секции унесло соседнюю: %v", cfg)
	}
}

// Внутри секции последнее слово за присланным: слияние ПОСЕКЦИОННОЕ, а не поключевое. Глубокое
// слияние сделало бы «убрать ключ» невыразимым, и это ровно та асимметрия, из-за которой снятая
// галочка когда-то ничего не отменяла (ADR-168).
func TestMergingIsPerSectionNotPerKey(t *testing.T) {
	s := fileTierServer(t)

	putSections(t, s, `{"settings":{"artifact_retention_days":7,"heal_llm":true}}`)
	putSections(t, s, `{"settings":{"artifact_retention_days":3}}`)

	cfg := configNow(t, s)
	st, ok := cfg["settings"].(map[string]any)
	if !ok {
		t.Fatalf("секция пропала: %v", cfg)
	}
	if got := st["artifact_retention_days"]; got != float64(3) {
		t.Errorf("новое значение не победило: %v", got)
	}
	if _, still := st["heal_llm"]; still {
		t.Errorf("секция слита ПОКЛЮЧЕВО: `heal_llm` пережил замену секции, значит убрать ключ "+
			"из настройки нечем — та же болезнь, что чинил ADR-168: %v", st)
	}
}

// Ответ обязан назвать, что уцелело, а не только что записано. Иначе «saved» подтверждает ровно то,
// что человек прислал, и молчит об остальном документе — а именно молчание и делало потерю секций
// невидимой.
func TestTheAnswerNamesWhatSurvivedTheSave(t *testing.T) {
	s := fileTierServer(t)

	putSections(t, s, `{"settings":{"artifact_retention_days":7}}`)
	body := putSections(t, s, `{"llm":{"backend":"openai"}}`)

	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	kept, ok := body["kept"].(map[string]any)
	if !ok {
		t.Fatalf("ответ не назвал уцелевшие секции (поле `kept`): %s", raw)
	}
	if kept["global"] != "settings" {
		t.Errorf("`kept.global` = %v, ожидалось \"settings\" — человек должен видеть, что его "+
			"сохранение НЕ тронуло: %s", kept["global"], raw)
	}
}
