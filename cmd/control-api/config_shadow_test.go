package main

// [ENV-SHADOWS-SAVED-SETTING-UNANNOUNCED] — сохранённая настройка, которую окружение процесса
// молча перекрывает, обязана быть НАЗВАНА в ответе, который человек читает.
//
// ЧТО БЫЛО ЗАМЕРЕНО. `resolveRunEnv` не перекрывает непустое значение процесса ни для персистентного
// слоя, ни для пер-ранного тела, и приоритет этот НАМЕРЕННЫЙ: развёртывание, пробрасывающее
// переменную с хоста, обязано продолжать ею пользоваться, иначе ключ, сохранённый через интерфейс,
// молча менял бы, куда уходит трафик. Дефект был не в приоритете, а в МОЛЧАНИИ: человек сохранял
// модель, получал «сохранено» и прогон на другой модели, и ни одна поверхность не говорила почему.
//
// ⚠ ЗАПИСЬ РЕЕСТРА ОПИСЫВАЛА ЭТОТ ПУТЬ ЧЕРЕЗ ЛОЖНУЮ ПОСЫЛКУ («`x-sentinel-base` вливается в сервис
// `control-api`»), и посылка опровергнута рендером эффективного конфига: собственный блок
// `environment:` замещает якорь целиком. Та половина — отдельная починка (compose + гейт якоря);
// здесь закрыта ровно вторая: молчание.
//
// ПОЧЕМУ ДВЕ НОГИ, А НЕ ОДНА. Утверждение «в ответе есть имя» само по себе вакуумно: его
// удовлетворил бы список, который печатает имена ВСЕГДА. Поэтому рядом стоит отрицательная нога —
// без переменной в окружении имени быть НЕ должно, — и обе привязаны к ФАКТУ: тот же
// `resolveRunEnv`, чьё поведение объявляется, здесь же проверяется.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func shadowedList(t *testing.T, s *server, tok string) []string {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/config", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /v1/config = %d (%s)", rec.Code, rec.Body.String())
	}
	var got struct {
		Shadowed []string `json:"shadowed_by_env"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("config body: %v (%s)", err, rec.Body.String())
	}
	return got.Shadowed
}

func TestASavedSettingTheProcessEnvOverridesIsAnnounced(t *testing.T) {
	s, admin, _, _ := storeBackedServer(t)
	if rec, _ := doJSON(t, s, http.MethodPut, "/v1/config",
		[]byte(`{"llm":{"model":"saved-model","backend":"openai"}}`), admin); rec.Code != http.StatusOK {
		t.Fatalf("сохранение секции llm: %d", rec.Code)
	}

	// 1. БЕЗ переменной в окружении затенения нет, и список обязан быть пуст. Без этой ноги
	// утверждение ниже удовлетворялось бы списком, который печатает имена всегда.
	for _, n := range shadowedList(t, s, admin) {
		if n == "LLM_MODEL" {
			t.Fatalf("имя объявлено затенённым, когда окружение его не несёт: %v", shadowedList(t, s, admin))
		}
	}

	// 2. С переменной — имя обязано быть названо.
	t.Setenv("LLM_MODEL", "host-model")
	named := false
	for _, n := range shadowedList(t, s, admin) {
		if n == "LLM_MODEL" {
			named = true
		}
	}
	if !named {
		t.Errorf("сохранённая модель перекрыта окружением, и ответ об этом молчит: %v — человек видит "+
			"«сохранено» и получает прогон на другой модели, не узнав почему", shadowedList(t, s, admin))
	}

	// 3. И ЭТО ПРАВДА, А НЕ ЯРЛЫК: тот же resolveRunEnv, чьё поведение объявляется, обязан
	// действительно оставить значение процесса. Объявление, разошедшееся с фактом, хуже молчания.
	got := resolveRunEnv([]string{"LLM_MODEL=host-model"}, nil, s.mergedPersistedEnv(), nil)
	for _, kv := range got {
		if kv == "LLM_MODEL=saved-model" {
			t.Errorf("объявлено затенение, которого нет: сохранённое значение применилось")
		}
	}
}
