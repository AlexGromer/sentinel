package main

// Гейт первого администратора и смены СВОЕГО пароля (ADR-159, W15).
//
// ⚠ ЧТО ИМЕННО ЗДЕСЬ СТЕРЕЖЁТСЯ. Заведение администратора при старте — не удобство, а изменение
// состояния личности развёртывания: с появлением ПЕРВОГО аккаунта закрываются маршруты `legacyOpen`,
// а обмен бутстрап-нонса начинает отвечать 409. Поэтому проверок на «не завёлся» здесь БОЛЬШЕ, чем
// на «завёлся»: лишний администратор, заведённый по ошибке (при моргнувшем хранилище, при уже
// существующих аккаунтах, при втором старте), — это не пропущенная возможность, а тихо изменённая
// модель доступа.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/AlexGromer/sentinel/internal/identity"
	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// adminFixture: сервер с настоящим хранилищем и без единого аккаунта.
func adminFixture(t *testing.T) *server {
	t.Helper()
	s := uiTestServer(t, enabledUI(t))
	addr := startTestGateway(t, "")
	sc, err := newStoreClient(addr, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sc.close)
	s.store = sc
	// `uiTestServer` не заводит хранилище сессий — оно нужно только тем проверкам, что работают с
	// личностью. Без него `mint` разыменовывает nil, и падение выглядит как дефект продукта.
	s.sessions = newSessionStore()
	s.forgetAccounts()
	return s
}

func TestDefaultAdminIsCreatedOnAnEmptyDeployment(t *testing.T) {
	s := adminFixture(t)
	s.ensureDefaultAdmin()

	u, ok := s.store.getUser(&storepb.UserRef{Name: defaultAdminName})
	if !ok || u == nil || !u.Found {
		t.Fatalf("администратор не заведён: ok=%v u=%+v", ok, u)
	}
	if !u.IsAdmin {
		t.Fatal("заведённый аккаунт НЕ администратор — развёртывание осталось бы без админа")
	}
	// Хранится ХЕШ, а не пароль. Утверждение о форме, а не о равенстве: пароль наружу не отдаётся
	// вовсе, поэтому сравнивать не с чем, а вот «в поле лежит нечто в формате хеша» проверяемо.
	if !identity.NeedsRehash(u.PwHash) && u.PwHash == "" {
		t.Fatal("пустой хеш пароля")
	}
	if len(u.PwHash) < 40 {
		t.Fatalf("в поле пароля лежит не хеш: %q", u.PwHash)
	}
	// Память стража обязана быть сброшена, иначе `legacyOpen` останется открыт в окне до 5 секунд,
	// которого по новой модели уже нет.
	if !s.accountsExist() {
		t.Fatal("администратор заведён, но страж всё ещё считает, что аккаунтов нет")
	}
}

func TestDefaultAdminIsNotCreatedTwice(t *testing.T) {
	s := adminFixture(t)
	s.ensureDefaultAdmin()
	s.ensureDefaultAdmin() // второй старт того же развёртывания
	s.ensureDefaultAdmin()

	list, ok := s.store.listUsers()
	if !ok {
		t.Fatal("хранилище не ответило")
	}
	if len(list.Users) != 1 {
		t.Fatalf("аккаунтов %d, ожидался ровно 1 — повторный старт завёл ещё одного администратора", len(list.Users))
	}
}

// ⚠ САМАЯ ВАЖНАЯ ИЗ ОТРИЦАТЕЛЬНЫХ. Развёртывание, где аккаунты УЖЕ есть, не должно получить
// администратора по умолчанию: это чужая система, и появление в ней `admin` с паролем в чьём-то
// терминале — не удобство, а лишний вход.
func TestDefaultAdminIsNotCreatedWhereAccountsAlreadyExist(t *testing.T) {
	s := adminFixture(t)
	existing := &storepb.User{UserId: "u-1", Name: "alice", PwHash: "not-a-real-hash", IsAdmin: false}
	if !s.store.upsertUser(existing) {
		t.Fatal("не удалось подготовить существующий аккаунт")
	}
	s.forgetAccounts()

	s.ensureDefaultAdmin()

	if u, ok := s.store.getUser(&storepb.UserRef{Name: defaultAdminName}); ok && u != nil && u.Found {
		t.Fatal("в развёртывании с существующими аккаунтами завёлся администратор по умолчанию")
	}
	list, _ := s.store.listUsers()
	if len(list.Users) != 1 {
		t.Fatalf("аккаунтов %d, ожидался ровно 1 (только alice)", len(list.Users))
	}
}

// Хранилища нет — заводить некуда, и это не ошибка. Отдельная проверка, потому что путь молчаливый:
// без неё регресс «падает при старте на автономном ярусе» прошёл бы незамеченным.
func TestDefaultAdminDoesNothingWithoutAStore(t *testing.T) {
	s := uiTestServer(t, enabledUI(t))
	s.store = nil
	s.ensureDefaultAdmin() // не должно паниковать и не должно ничего делать
}

// Пароль ГЕНЕРИРУЕТСЯ. Два вызова обязаны разойтись — фиксированный пароль по умолчанию был
// отклонён именно потому, что он одинаков у всех развёртываний.
func TestDefaultAdminPasswordIsGeneratedNotFixed(t *testing.T) {
	a, ok1 := newDefaultAdminPassword()
	b, ok2 := newDefaultAdminPassword()
	if !ok1 || !ok2 {
		t.Fatal("генератор пароля не отдал значение")
	}
	if a == b {
		t.Fatalf("два пароля совпали (%q) — пароль не случайный", a)
	}
	if len(a) < 24 {
		t.Fatalf("пароль слишком короток: %d знаков (%q)", len(a), a)
	}
}

/* ------------------------------------------------------------------ смена пароля */

func changePassword(t *testing.T, s *server, cred, current, next string) *httptest.ResponseRecorder {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"current_password": current, "new_password": next})
	req := httptest.NewRequest(http.MethodPost, "/v1/me/password", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if cred != "" {
		req.Header.Set("Authorization", "Bearer "+cred)
	}
	rec := httptest.NewRecorder()
	s.mux().ServeHTTP(rec, req)
	return rec
}

// Один сценарий, потому что все ветки отказа принадлежат ОДНОМУ состоянию (аккаунт с известным
// паролем и живая сессия): разнесение по функциям потребовало бы четырежды поднять хранилище ради
// проверок, которые различаются одной строкой тела.
func TestChangePasswordGuardsEveryWay(t *testing.T) {
	s := adminFixture(t)
	s.ensureDefaultAdmin()
	u, _ := s.store.getUser(&storepb.UserRef{Name: defaultAdminName})

	// Пароль наружу не отдаётся, поэтому для проверки задаём известный явно — это ровно то, что
	// делает `handleChangePassword`, и по тому же пути.
	const known = "the-known-passphrase"
	h, err := identity.Hash(known)
	if err != nil {
		t.Fatal(err)
	}
	u.PwHash = h
	if !s.store.upsertUser(u) {
		t.Fatal("не удалось подготовить пароль")
	}
	sess := s.sessions.mint(u.UserId, u.Name, u.IsAdmin, sessionTTL())
	if sess == "" {
		t.Fatal("сессия не выдана")
	}

	if rec := changePassword(t, s, "", known, "a-brand-new-passphrase"); rec.Code != http.StatusForbidden {
		t.Errorf("без кредентиала = %d, ожидался 403", rec.Code)
	}
	if rec := changePassword(t, s, s.token, known, "a-brand-new-passphrase"); rec.Code != http.StatusForbidden {
		t.Errorf("машинным токеном = %d, ожидался 403 — у машины нет аккаунта", rec.Code)
	}
	if rec := changePassword(t, s, sess, "not-the-current-one", "a-brand-new-passphrase"); rec.Code != http.StatusUnauthorized {
		t.Errorf("с неверным текущим паролем = %d, ожидался 401", rec.Code)
	}
	if rec := changePassword(t, s, sess, known, "short"); rec.Code != http.StatusBadRequest {
		t.Errorf("со слишком коротким новым = %d, ожидался 400", rec.Code)
	}
	// Ни один отказ не имел права поменять пароль: иначе первая же неудачная попытка оставляла бы
	// аккаунт в состоянии, которого не просили.
	after, _ := s.store.getUser(&storepb.UserRef{UserId: u.UserId})
	if !identity.Verify(after.PwHash, known) {
		t.Fatal("после отказов пароль изменился")
	}

	rec := changePassword(t, s, sess, known, "a-brand-new-passphrase")
	if rec.Code != http.StatusOK {
		t.Fatalf("успешная смена = %d (%s)", rec.Code, rec.Body.String())
	}
	var out struct {
		Status  string `json:"status"`
		Session string `json:"session"`
	}
	if json.Unmarshal(rec.Body.Bytes(), &out) != nil || out.Session == "" {
		t.Fatalf("ответ без свежей сессии: %s", rec.Body.String())
	}
	// ⚠ СТАРЫЕ СЕССИИ ПОГАШЕНЫ — в этом смысл смены пароля. Без этой строки проверка была бы зелёной
	// над реализацией, оставляющей ровно тот доступ, ради закрытия которого пароль и меняют.
	if _, ok := s.sessions.lookup(sess); ok {
		t.Error("сессия, выданная под старым паролем, пережила смену")
	}
	if _, ok := s.sessions.lookup(out.Session); !ok {
		t.Error("свежая сессия из ответа не работает")
	}
	final, _ := s.store.getUser(&storepb.UserRef{UserId: u.UserId})
	if !identity.Verify(final.PwHash, "a-brand-new-passphrase") {
		t.Error("новый пароль не сохранён")
	}
	if identity.Verify(final.PwHash, known) {
		t.Error("старый пароль всё ещё подходит")
	}
}
