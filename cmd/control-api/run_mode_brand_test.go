package main

// [RUN-MODE-LABELS-THE-RECORD-NOT-THE-RUN] — клеймо прогона против того, что РЕАЛЬНО ушло в argv.
//
// ЧТО БЫЛО ЗАМЕРЕНО. `rec.Mode` копировался из `req.Mode` — строки запроса, которую никакая
// валидация с перечнем не сверяла, — и врал в ОБЕ стороны. Человек выбирал «Цель», оставлял поле
// цели пустым, жал «Прогон»: `--goal` в argv не попадал, прогон шёл обычным explore, а строка
// прогона говорила `mode=goal`. И наоборот: ход в разговоре уезжал с `--mode chat`, а записывался
// как `describe`, потому что таким пришёл в теле. Клеймо не косметика — оно доезжает до GET
// /v1/runs, до `ResultRecord.Mode` и до меток метрик `{mode,target}`, то есть ложь здесь есть ложь
// в записи, в отчёте и на приборной панели сразу.
//
// ⚠ ОСЕЙ ДВЕ, И ЭТО ЗАМЕР, А НЕ ДОПУЩЕНИЕ. Собственный вывод agentctl на `--goal` — `mode=explore`:
// ось ИСПОЛНЕНИЯ (RUN_MODE: explore/replay/baseline/chat) и ось АВТОРИНГА (goal/describe, ADR-027 —
// её несут `--goal`/`--describe`, а `--mode goal` отклонён) — разные величины. Поэтому гейт НЕ
// требует равенства клейма и `mode=` агента: такое требование было бы неверно для двух значений из
// шести. Он требует, чтобы клеймо было ПОДКРЕПЛЕНО маркером в argv — артефакте, который читает
// другой процесс.
//
// ЗНАМЕНАТЕЛЬ ВЫВОДИТСЯ: перечень режимов берётся из живой схемы, и каждое его значение обязано
// быть покрыто случаем ниже. Таблица, отставшая от перечня, краснеет вместо того, чтобы молча
// проверять на один режим меньше.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func brandOf(t *testing.T, s *server, id string) string {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/runs/"+id, nil)
	req.Header.Set("Authorization", "Bearer secret-tok")
	s.mux().ServeHTTP(rec, req)
	var got map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("run body: %v (%s)", err, rec.Body.String())
	}
	m, _ := got["mode"].(string)
	return m
}

func TestTheRunRecordIsBrandedByWhatReallyRan(t *testing.T) {
	cases := []struct {
		name  string
		body  map[string]string
		prior string // непустое — посеять замороженный план под этим id
		brand string
		// marker — то, что ОБЯЗАНО быть в argv, чтобы клеймо имело основание. Пустая строка означает
		// «обосновано ОТСУТСТВИЕМ маркеров авторинга», и такой случай проверяется отдельно ниже.
		marker string
	}{
		{
			// ТОТ САМЫЙ ДЕФЕКТ: намерение объявлено, цель пуста, `--goal` не уехал.
			name:  "выбрана Цель, но цель пуста — прогон обычный explore",
			body:  map[string]string{"target": "https://app.example", "mode": "goal"},
			brand: "explore",
		},
		{
			name:   "цель заполнена — прогон действительно авторит по цели",
			body:   map[string]string{"target": "https://app.example", "mode": "goal", "goal": "купить товар"},
			brand:  "goal",
			marker: "--goal",
		},
		{
			name:   "описание заполнено",
			body:   map[string]string{"target": "https://app.example", "mode": "describe", "describe": "поток оплаты"},
			brand:  "describe",
			marker: "--describe",
		},
		{
			// ВТОРАЯ СТОРОНА ЛЖИ: настоящий ход разговора, помеченный в теле как describe.
			name:   "ход разговора помечен describe — прогон всё равно chat",
			body:   map[string]string{"target": "https://app.example", "mode": "describe", "conversation_id": "c1", "message": "привет"},
			brand:  "chat",
			marker: "--mode",
		},
		{
			name:   "воспроизведение",
			body:   map[string]string{"mode": "replay", "from_run": "prior-replay"},
			prior:  "prior-replay",
			brand:  "replay",
			marker: "--replay",
		},
		{
			name:   "обновление эталона",
			body:   map[string]string{"mode": "baseline", "from_run": "prior-baseline"},
			prior:  "prior-baseline",
			brand:  "baseline",
			marker: "baseline",
		},
	}

	covered := map[string]bool{}
	for _, c := range cases {
		covered[c.brand] = true
		t.Run(c.name, func(t *testing.T) {
			s, repo, argvPath := newArgvCapturingServer(t, 0)
			if c.prior != "" {
				seedPriorPlan(t, repo, c.prior, "plan.json",
					`{"target_url":"https://app.example","plan_id":"p1","plan_hash":"h","steps":[]}`)
			}
			id := postRunAndWait(t, s, runBody(t, c.body))
			argv := readArgv(t, argvPath)
			joined := strings.Join(argv, " ")

			if got := brandOf(t, s, id); got != c.brand {
				t.Errorf("клеймо %q, а прогон шёл как %q — запись расходится с прогоном: %s", got, c.brand, joined)
			}
			// Вторая нога: клеймо обязано быть ПОДКРЕПЛЕНО argv, иначе оно снова стало бы пересказом
			// поля запроса, просто вычисленным в другом месте.
			if c.marker != "" && !strings.Contains(joined, c.marker) {
				t.Errorf("клеймо %q не подкреплено argv (нет %q): %s", c.brand, c.marker, joined)
			}
			if c.brand == "explore" {
				for _, forbidden := range []string{"--goal", "--describe", "--replay", "--mode"} {
					if strings.Contains(joined, forbidden) {
						t.Errorf("клеймо explore при маркере %q в argv: %s", forbidden, joined)
					}
				}
			}
		})
	}

	// ЗНАМЕНАТЕЛЬ СНАРУЖИ ТАБЛИЦЫ. Перечень режимов публикует схема; таблица выше обязана покрывать
	// его целиком. Без этого добавленный режим молча остался бы непроверенным — а «пропущенное не
	// имеет представления, на которое можно посмотреть» (docs/DEVELOPMENT.md §0, принцип 5).
	s := newTestServer()
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/config-schema", nil))
	var sc map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &sc); err != nil {
		t.Fatalf("schema: %v", err)
	}
	fields, _ := sc["fields"].(map[string]any)
	mode, _ := fields["mode"].(map[string]any)
	enum, _ := mode["enum"].([]any)
	if len(enum) < 6 {
		t.Fatalf("схема публикует %d режимов — сравнивать таблицу не с чем", len(enum))
	}
	for _, v := range enum {
		name, _ := v.(string)
		if !covered[name] {
			t.Errorf("режим %q публикуется схемой и не покрыт ни одним случаем — клеймо для него никто не проверяет", name)
		}
	}
}
