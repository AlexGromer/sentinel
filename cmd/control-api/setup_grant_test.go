package main

// Гейт: первый запуск НЕ отдаёт браузеру машинный токен, и право первичной настройки не умеет ничего,
// кроме заведения первого администратора (ADR-156, W15).
//
// ⚠ ЧТО БЫЛО. Обмен одноразового нонса возвращал странице `{"token": s.token}` — МАШИННЫЙ кредентиал:
// незаскоупленный (`access.go`: «машина проходит всё»), постоянный, общий с CI и неотзываемый
// поштучно. Механика нонса при этом хороша и не менялась; менялось то, во ЧТО он обменивается.
//
// ⚠ ПОЧЕМУ ГРАНИЦЫ ПРАВА ПРОВЕРЯЮТСЯ ЗДЕСЬ ПОШТУЧНО. Право — это третий способ удовлетворить
// `accessAdmin`, то есть новая дверь в стене, которую всё остальное в этом файле стережёт. Дверь,
// открытая шире, чем задумано, выглядит точно как работающая: заводить администратора она будет и в
// том случае, если заодно пускает на удаление аккаунтов. Поэтому каждая граница — отдельная проверка,
// и все они отрицательные.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// grantFixture: сервер с UI и хранилищем, нонс обменян, право на руках.
func grantFixture(t *testing.T) (*server, string) {
	t.Helper()
	s := uiTestServer(t, enabledUI(t))
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sc.close)
	s.store = sc
	s.forgetAccounts()

	nonce := s.ui.arm(time.Minute)
	rec := get(t, s.mux(), "/v1/ui-token?nonce="+nonce)
	if rec.Code != http.StatusOK {
		t.Fatalf("обмен нонса = %d (%s)", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	grant, _ := body["setup"].(string)
	if grant == "" {
		t.Fatalf("право не выдано: %s", rec.Body.String())
	}
	return s, grant
}

func postAs(t *testing.T, s *server, path, grant, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	if grant != "" {
		req.Header.Set(setupGrantHeader, grant)
	}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	return rec
}

func TestTheSetupGrantCreatesTheFirstAdministrator(t *testing.T) {
	// KILLS: снятие права из стража (создание первого аккаунта снова потребует машинного токена, и
	// первый запуск снова будет вынужден выдать его браузеру).
	s, grant := grantFixture(t)
	rec := postAs(t, s, "/v1/users", grant, `{"name":"root","password":"a-long-enough-passphrase"}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("создание первого администратора = %d (%s)", rec.Code, rec.Body.String())
	}
	var u map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &u); err != nil {
		t.Fatal(err)
	}
	// ⚠ Тело НЕ просило админа. Право обязано принудить: иначе окно закроется первым же аккаунтом, а
	// администратора в развёртывании не окажется — тупик, ради ухода от которого право и заводилось.
	if admin, _ := u["is_admin"].(bool); !admin {
		t.Error("аккаунт заведён НЕ администратором — развёртывание осталось бы без админа")
	}
}

func TestTheGrantOpensNothingElse(t *testing.T) {
	// Граница по МАРШРУТУ. KILLS: расширение права на любой другой accessAdmin-маршрут.
	s, grant := grantFixture(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/users", nil)
	req.Header.Set(setupGrantHeader, grant)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Errorf("GET /v1/users по праву настройки = %d, ожидался 403 — право умеет ровно одно", rec.Code)
	}
}

func TestTheGrantDiesWithTheFirstAccount(t *testing.T) {
	// Граница по ВРЕМЕНИ ЖИЗНИ, и она двойная: право гасится после успеха И окно закрывается тем, что
	// аккаунт теперь существует. KILLS: снятие burnGrant; снятие условия !accountsExist() у стража.
	s, grant := grantFixture(t)
	if rec := postAs(t, s, "/v1/users", grant, `{"name":"root","password":"a-long-enough-passphrase"}`); rec.Code != http.StatusCreated {
		t.Fatalf("первый аккаунт не создан: %d (%s)", rec.Code, rec.Body.String())
	}
	rec := postAs(t, s, "/v1/users", grant, `{"name":"second","password":"a-long-enough-passphrase"}`)
	if rec.Code != http.StatusForbidden {
		t.Errorf("второй аккаунт тем же правом = %d, ожидался 403 — право одноразово", rec.Code)
	}
}

func TestAForgedGrantIsRefused(t *testing.T) {
	// Встречная проверка: без неё три проверки выше удовлетворялись бы сервером, который пускает
	// вообще всех. KILLS: сравнение, принимающее пустое или произвольное значение.
	s, _ := grantFixture(t)
	for _, bad := range []string{"", "deadbeef", strings.Repeat("0", 2*uiNonceBytes)} {
		rec := postAs(t, s, "/v1/users", bad, `{"name":"root","password":"a-long-enough-passphrase"}`)
		if rec.Code != http.StatusForbidden {
			t.Errorf("подделанное право %q = %d, ожидался 403", bad, rec.Code)
		}
	}
}

func TestTheExchangeRefusesOnceAccountsExistAndKeepsTheNonce(t *testing.T) {
	// ⚠ ОЧЕРЁДНОСТЬ ПРОВЕРОК — САМА ПО СЕБЕ СВОЙСТВО. Нонс одноразовый; сжечь его в ответ на запрос,
	// который всё равно нечем удовлетворить, значит заставить оператора перезапустить сервер из-за
	// нашей же очерёдности. Поэтому состояние развёртывания проверяется ДО обмена.
	// KILLS: перестановка `accountsExist()` после `redeem()`.
	s := uiTestServer(t, enabledUI(t))
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	defer sc.close()
	s.store = sc
	s.store.upsertUser(&storepb.User{UserId: "u1", Name: "someone", PwHash: "x"})
	s.forgetAccounts()

	nonce := s.ui.arm(time.Minute)
	rec := get(t, s.mux(), "/v1/ui-token?nonce="+nonce)
	if rec.Code != http.StatusConflict {
		t.Errorf("обмен при существующих аккаунтах = %d, ожидался 409", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "/v1/login") {
		t.Errorf("отказ не называет, что делать вместо этого: %s", rec.Body.String())
	}
	if s.ui.nonce == "" {
		t.Error("нонс сожжён отказом, который его не касался — оператору придётся перезапускать сервер")
	}
}

func TestTheWindowClosesEvenIfTheGrantWasNeverBurned(t *testing.T) {
	// ⚠ ЭТА ПРОВЕРКА ЗАВЕДЕНА ПО ВЫЖИВШЕЙ МУТАЦИИ, и это разный вопрос, а не тот же самый.
	//
	// Окно первичной настройки закрывают ДВЕ независимые вещи: `burnGrant` после успеха и условие
	// `!accountsExist()` у стража. Мутация, снявшая ВТОРУЮ, прошла зелёной — потому что в сценарии
	// «завёл администратора этим же правом» первая срабатывает раньше и тест не может сказать, какая
	// из них его закрыла. Здесь аккаунт заводится ДРУГИМ путём (машинным токеном), право при этом не
	// гасится вовсе, и остаётся ровно одна работающая защита — та, которую мутация и снимала.
	//
	// KILLS: снятие `!s.accountsExist()` из условия права в access.go.
	s, grant := grantFixture(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/users",
		bytes.NewBufferString(`{"name":"byMachine","password":"a-long-enough-passphrase"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+s.token)
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("аккаунт машинным токеном не создан: %d (%s)", rec.Code, rec.Body.String())
	}
	// Право НЕ гасилось: его никто не предъявлял. Работать оно тем не менее больше не должно.
	if !s.ui.checkGrant(grant) {
		t.Fatal("право уже погашено — проверка не про то, что задумано (пересмотрите фикстуру)")
	}
	if rec := postAs(t, s, "/v1/users", grant, `{"name":"sneak","password":"a-long-enough-passphrase"}`); rec.Code != http.StatusForbidden {
		t.Errorf("годное право сработало при существующих аккаунтах = %d, ожидался 403", rec.Code)
	}
}
