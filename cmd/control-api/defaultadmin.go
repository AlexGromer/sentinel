// Первый администратор заводится САМ, а его пароль печатается ровно один раз (ADR-159).
//
// РЕШЕНИЕ ALEX (2026-09-06): «стандартный админ есть сразу, можно ему менять пароль, и менять свои
// пароли могут пользователи после входа», и отдельно — пароль ГЕНЕРИРУЕТСЯ и печатается один раз, а
// не берётся фиксированным из документации.
//
// ⚠ ПОЧЕМУ НЕ ФИКСИРОВАННЫЙ ПАРОЛЬ. Известный пароль по умолчанию — не удобство, а окно: между
// стартом развёртывания и тем моментом, когда оператор дойдёт до его смены, администратором является
// каждый, кто дотянулся до порта. Сгенерированный даёт то же удобство («аккаунт уже есть, войдите»)
// без этого окна, и канал доставки у него уже построен — тот же терминал оператора, куда печатается
// одноразовый бутстрап-нонс, и по той же причине: это единственное место, которое видит человек,
// поднявший сервис, и не видит никто другой.
//
// ⚠ ЧТО ЭТО МЕНЯЕТ, И ЭТО НАДО ЧИТАТЬ КАК ЧАСТЬ РЕШЕНИЯ. Появление ПЕРВОГО аккаунта закрывает
// маршруты с `legacyOpen` — их ровно два, `GET /v1/runs` и `GET /v1/runs/{id}`. То есть свежее
// развёртывание перестаёт отдавать список прогонов без кредентиала. Это следствие, а не побочный
// эффект: до сих пор «аккаунтов ещё нет» было НЕОТЛИЧИМО от «здесь так и задумано», и анонимное
// чтение держалось ровно до первого заведённого человека. Теперь ответ один и тот же с первой
// секунды. Запасной путь на случай, если пароль потерян: машинный токен в `state/control-api.token`
// (`chmod 0600`, читается с хоста) — он никуда не делся и по-прежнему проходит везде.
package main

import (
	"crypto/rand"
	"encoding/base32"
	"fmt"
	"os"
	"strings"

	"github.com/AlexGromer/sentinel/internal/identity"
	storepb "github.com/AlexGromer/sentinel/internal/store/pb"
)

// defaultAdminName — имя, а не секрет. Оно ПРЕДСКАЗУЕМО намеренно: человеку, читающему строку
// старта, надо набрать его в форме входа, и заставлять его запоминать ещё и случайное имя значило бы
// усложнить единственный шаг, ради упрощения которого всё это заводится. Защищает пароль.
const defaultAdminName = "admin"

// defaultAdminPasswordBytes — 20 байт (160 бит) сырой энтропии. base32 без набивки даёт 32 знака,
// которые человек может перенабрать с экрана: hex при той же энтропии длиннее, а base64 несёт `+/`
// и регистр, который в терминале путают. Перебор здесь не при чём — пароль живёт до первой смены, но
// смены может и не случиться, поэтому он обязан выдерживать её отсутствие.
const defaultAdminPasswordBytes = 20

// newDefaultAdminPassword возвращает пароль и признак успеха. Пустая строка — это НЕ «пустой пароль»,
// а «энтропии не нашлось», и вызыватель обязан на этом остановиться: завести администратора с
// предсказуемым секретом хуже, чем не завести его вовсе.
func newDefaultAdminPassword() (string, bool) {
	b := make([]byte, defaultAdminPasswordBytes)
	if _, err := rand.Read(b); err != nil {
		return "", false
	}
	return strings.ToLower(base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(b)), true
}

// ensureDefaultAdmin заводит первого администратора, если аккаунтов в развёртывании ещё нет.
//
// Молчит и ничего не делает, когда: хранилища нет (заводить некуда), аккаунты уже есть (в том числе
// когда список НЕ ПРИШЁЛ — см. ниже), или не удалось получить энтропию.
//
// ⚠ ПРОВЕРЯЕМ ПО ПОЛОЖИТЕЛЬНОМУ ЗНАНИЮ, А НЕ ЧЕРЕЗ `accountsExist()`. Тот при недоступном хранилище
// отвечает «аккаунты есть» — осторожность, верная для СТРАЖА, потому что иначе сбой транспорта
// переоткрывал бы `legacyOpen`. Здесь нужен ровно обратный вопрос: «точно ли их нет», и ошибиться в
// сторону «нет» означало бы завести ВТОРОГО администратора при каждом старте с моргнувшим
// хранилищем. Поэтому список запрашивается напрямую, и любой ответ, кроме уверенно пустого,
// означает «ничего не делаем».
func (s *server) ensureDefaultAdmin() {
	if s.store == nil {
		return
	}
	list, ok := s.store.listUsers()
	if !ok || list == nil {
		// Хранилище не ответило. Не «аккаунтов нет» — «неизвестно», и на неизвестном не заводят.
		return
	}
	if len(list.Users) > 0 {
		return
	}
	pw, ok := newDefaultAdminPassword()
	if !ok {
		fmt.Fprintln(os.Stderr, "control-api: WARNING — could not generate a password for the first "+
			"administrator (no entropy); no account was created. Use the machine token from the token file.")
		return
	}
	hash, err := identity.Hash(pw)
	if err != nil {
		fmt.Fprintf(os.Stderr, "control-api: WARNING — could not hash the first administrator's password: %v\n", err)
		return
	}
	u := &storepb.User{UserId: newRunID(), Name: defaultAdminName, PwHash: hash, IsAdmin: true}
	if !s.store.upsertUser(u) {
		fmt.Fprintln(os.Stderr, "control-api: WARNING — the store did not accept the first administrator; "+
			"no account was created")
		return
	}
	// Память стража помнит «аккаунтов нет» до 5 секунд. Без сброса первые запросы после старта шли бы
	// по устаревшему ответу, то есть `legacyOpen` оставался бы открыт в окне, которого уже нет.
	s.forgetAccounts()

	// ⚠ ПАРОЛЬ ПЕЧАТАЕТСЯ ЗДЕСЬ И БОЛЬШЕ НИГДЕ. В журнал сервиса он не идёт: журнал читается через
	// интерфейс, переживает перезапуск и уезжает в артефакты — то есть ровно то, чем одноразовость
	// не является. По той же причине его нет ни в одном ответе API.
	fmt.Fprintf(os.Stderr,
		"control-api: created the first administrator %q with a generated password: %s\n"+
			"control-api:   this is printed ONCE and stored only as a hash — change it at any time with "+
			"POST /v1/me/password, or from the hub's Settings view\n",
		defaultAdminName, pw)
	// В журнал идёт ФАКТ, а не секрет: без него «откуда взялся аккаунт admin» станет вопросом, на
	// который развёртывание не отвечает (принцип 7 — компонент, о котором ничего не записано).
	s.journalEvent("service.default_admin_created", "info",
		map[string]string{"actor": defaultAdminName}, nil)
}
