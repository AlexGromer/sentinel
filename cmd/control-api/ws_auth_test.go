package main

// Гейт: вход в аккаунт НЕ ДОЛЖЕН отбирать у страницы живой вид, и починка этого не должна открывать
// чужие прогоны.
//
// ⚠ ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ — ДЕФЕКТ БЫЛ ЗАМЕРЕН ЖИВЬЁМ, А НЕ ПРЕДПОЛОЖЕН (2026-09-06, работающее
// развёртывание, ADR-155). `wsAuthed` сверял ТОЛЬКО машинный токен и о сессиях не знал ничего, а хаб
// после `POST /v1/login` кладёт СЕССИЮ в то самое поле, которым подписывает запросы
// (docs/index.html:2308), и шлёт её подпротоколом `bearer.<cred>` на оба WebSocket-маршрута. Итог:
// вошёл в аккаунт → 403 и на живом экране, и на живом таймлайне. Вход ПОНИЖАЛ возможности страницы.
//
// ⚠ И ВТОРОЙ ДЕФЕКТ ТАМ ЖЕ: `/v1/stream` стоял `accessAuthed + legacyOpen`, поэтому с появлением
// ПЕРВОГО аккаунта страж требовал заголовок `Authorization`, которого браузерный WebSocket выставить
// не может в принципе, и отвечал 403 ДО обработчика — даже держателю машинного токена. Замерено на
// живом стенде: тот же handshake С заголовком → 101, только подпротоколом → 403.
//
// ⚠ ПОЧЕМУ НИ ОДИН СУЩЕСТВУЮЩИЙ ГЕЙТ ЭТОГО НЕ ЛОВИЛ, И ПОЧЕМУ ЗДЕСЬ НАСТОЯЩИЙ СЕРВЕР. Соседние
// проверки строят запрос через `httptest.NewRecorder`, а он НЕ реализует `http.Hijacker` — то есть
// путь, на котором рукопожатие УДАЁТСЯ, через рекордер непроходим в принципе, и проверялись только
// отказы. Поэтому здесь поднимается настоящий `httptest.NewServer` и делается настоящее рукопожатие:
// иначе гейт был бы зелёным ровно над тем, ради чего заводится.
//
// ⚠ И ГЛАВНОЕ ПРО САМУ ПОЧИНКУ: принять сессию и не заскоупить — значит превратить отказ в УТЕЧКУ.
// `mayTouch` переиспользовать нельзя, он читает id из `r.PathValue("id")`, а эти маршруты принимают
// прогон в СТРОКЕ ЗАПРОСА (`?run_id=`) — объявление домена в таблице не заскоупило бы НИЧЕГО, молча.

import (
	"bufio"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// wsHandshake делает НАСТОЯЩЕЕ рукопожатие и возвращает код ответа. Заголовок `Authorization` НЕ
// шлётся намеренно: браузер его выставить не может, и вся суть проверки в том, что продукт работает
// в тех условиях, в которых работает браузер.
func wsHandshake(t *testing.T, base, path, cred string) int {
	t.Helper()
	u := strings.TrimPrefix(base, "http://")
	conn, err := net.Dial("tcp", u)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	k := make([]byte, 16)
	if _, err := rand.Read(k); err != nil {
		t.Fatalf("rand: %v", err)
	}
	req := fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"+
		"Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: %s%s\r\n\r\n",
		path, u, base64.StdEncoding.EncodeToString(k), wsTokenProto, cred)
	if _, err := conn.Write([]byte(req)); err != nil {
		t.Fatalf("write: %v", err)
	}
	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	defer resp.Body.Close()
	return resp.StatusCode
}

// wsFixture: сервер с хранилищем, одним аккаунтом и его сессией; возвращает адрес и всё нужное.
func wsFixture(t *testing.T) (*server, string, storepb.StoreServiceClient, func()) {
	t.Helper()
	s := newTestServer()
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	s.store = sc
	raw := rawStore(t, addr)
	srv := httptest.NewServer(s.mux())
	return s, srv.URL, raw, func() { srv.Close(); sc.close() }
}

func sessionFor(t *testing.T, s *server, raw storepb.StoreServiceClient, id, name string) string {
	t.Helper()
	s.store.upsertUser(&storepb.User{UserId: id, Name: name, PwHash: "x"})
	s.forgetAccounts()
	return s.sessions.mint(id, name, false, time.Hour)
}

func TestSigningInDoesNotCostThePageItsLiveView(t *testing.T) {
	// РЕГРЕСС, ЗАМЕРЕННЫЙ ЖИВЬЁМ: до починки обе строки давали 403, потому что `wsAuthed` знал только
	// машинный токен. KILLS: возврат к сверке ровно с `s.token` в разрешении WebSocket-кредентиала.
	s, base, raw, done := wsFixture(t)
	defer done()
	sess := sessionFor(t, s, raw, "ua", "alice")

	for _, path := range []string{"/v1/stream?run_id=", "/v1/live/screen"} {
		if code := wsHandshake(t, base, path, sess); code == http.StatusForbidden {
			t.Errorf("%s: сессия получила 403 — вход в аккаунт снова отбирает у страницы живой вид", path)
		}
	}
}

func TestABrowserHandshakeNeedsNoAuthorizationHeader(t *testing.T) {
	// ВТОРОЙ ДЕФЕКТ: страж требовал заголовок, которого браузерный WebSocket выставить не может, и
	// отказывал ДО обработчика — в том числе держателю машинного токена, как только заводился первый
	// аккаунт. `wsHandshake` заголовок не шлёт вовсе, поэтому проверка меряет ровно условия браузера.
	// KILLS: возврат `/v1/stream` к `accessAuthed` (страж снова потребует заголовок).
	s, base, raw, done := wsFixture(t)
	defer done()
	sessionFor(t, s, raw, "ua", "alice") // ⚠ аккаунт ЕСТЬ — именно он закрывал legacyOpen

	if code := wsHandshake(t, base, "/v1/stream?run_id=", s.token); code == http.StatusForbidden {
		t.Error("/v1/stream: машинный токен в подпротоколе получил 403 — страж снова требует заголовок")
	}
}

func TestAnUnknownCredentialIsStillRefused(t *testing.T) {
	// Встречная проверка: починка не должна открыть дверь. Без неё две проверки выше удовлетворялись бы
	// сервером, который пускает вообще всех.
	s, base, raw, done := wsFixture(t)
	defer done()
	sessionFor(t, s, raw, "ua", "alice")

	for _, path := range []string{"/v1/stream?run_id=", "/v1/live/screen"} {
		if code := wsHandshake(t, base, path, "not-a-credential"); code != http.StatusForbidden {
			t.Errorf("%s: мусорный кредентиал получил %d, ожидался 403", path, code)
		}
	}
}

func TestAStreamIsScopedToItsOwner(t *testing.T) {
	// ⚠ САМОЕ ВАЖНОЕ ЗДЕСЬ. Принять сессию и не заскоупить — значит превратить отказ в УТЕЧКУ: любой
	// вошедший смотрел бы чужой прогон. Замерено живьём после починки: свой 101, чужой 403, машина 101.
	// KILLS: снятие `wsMayStream`; попытка заскоупить через `mayTouch` (он читает id из ПУТИ, а здесь
	// прогон приходит строкой запроса — заскоупило бы НОЛЬ, и молча).
	s, base, raw, done := wsFixture(t)
	defer done()
	alice := sessionFor(t, s, raw, "ua", "alice")
	bob := sessionFor(t, s, raw, "ub", "bob")
	runID := seedRow(t, s, raw, domainRun, "ua")

	if code := wsHandshake(t, base, "/v1/stream?run_id="+runID, alice); code == http.StatusForbidden {
		t.Error("владелец не смог смотреть СВОЙ прогон")
	}
	if code := wsHandshake(t, base, "/v1/stream?run_id="+runID, bob); code != http.StatusForbidden {
		t.Errorf("ЧУЖОЙ прогон отдан другому аккаунту (%d) — это утечка, а не отказ", code)
	}
	if code := wsHandshake(t, base, "/v1/stream?run_id="+runID, s.token); code == http.StatusForbidden {
		t.Error("машинный токен заскоуплен — CI и agentctl обязаны видеть всё (mayTouch: c.machine)")
	}
}

func TestBothWebSocketRoutesGuardTheSameWay(t *testing.T) {
	// Свойство ВЫВОДИТСЯ из таблицы, а не пересказывается: у обоих маршрутов кредентиал проверяет
	// ОБРАБОТЧИК, поэтому у стража они обязаны стоять одинаково. Здесь стояло `accessAuthed` против
	// `accessOpen`, а комментарий рядом утверждал «the same arrangement» — ложное заявление, которое
	// и было вторым дефектом. KILLS: расхождение уровней у этих двух маршрутов в любую сторону.
	s := newTestServer()
	want := map[string]accessMode{}
	for _, sp := range s.routes() {
		if sp.pattern == "GET /v1/stream" || sp.pattern == "GET /v1/live/screen" {
			want[sp.pattern] = sp.access
			if sp.legacyOpen {
				t.Errorf("%s: legacyOpen на WebSocket-маршруте — он перестаёт послаблять на первом же "+
					"аккаунте и возвращает 403 браузеру, который заголовок выставить не может", sp.pattern)
			}
		}
	}
	if len(want) != 2 {
		t.Fatalf("ожидались оба WebSocket-маршрута, найдено %d: %v", len(want), want)
	}
	if want["GET /v1/stream"] != want["GET /v1/live/screen"] {
		t.Errorf("маршруты стерегутся по-разному (%v) — один из них снова отдаст браузеру 403 до "+
			"обработчика", want)
	}
}
