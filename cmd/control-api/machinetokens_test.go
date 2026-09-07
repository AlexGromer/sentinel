package main

// Гейт именованных машинных токенов и пола операторского токена (ADR-160, W15).
//
// ⚠ ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ В ПЕРВУЮ ОЧЕРЕДЬ. Токен — это дверь, и у неё три свойства, каждое из
// которых по отдельности выглядит работающим: значение показывается ОДИН раз · отозванный ПЕРЕСТАЁТ
// пускать · отзыв одного НЕ трогает остальных. Проверка только первого прошла бы над реализацией,
// которая ничего не отзывает; проверка только второго — над той, что отзывает ВСЕ.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func mtFixture(t *testing.T) *server {
	t.Helper()
	return &server{repo: t.TempDir(), token: "server-machine-token-long"}
}

func TestMachineTokenIsShownOnceAndStoredAsAHash(t *testing.T) {
	s := mtFixture(t)
	rec, value, err := s.issueMachineToken("ci")
	if err != nil {
		t.Fatal(err)
	}
	if value == "" || len(value) != machineTokenBytes*2 {
		t.Fatalf("значение токена имеет неверную форму: %q", value)
	}
	// ⚠ ГЛАВНОЕ: на диске значения НЕТ. Утверждение снимается с САМОГО ФАЙЛА, а не с ответа функции —
	// именно файл переживает процесс и именно его читает всякий, кто дотянулся до `state/`.
	raw, err := os.ReadFile(s.machineTokensPath())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), value) {
		t.Fatal("значение токена лежит на диске — хранить полагалось только хеш")
	}
	if !strings.Contains(string(raw), rec.Hash) {
		t.Fatal("хеша токена в файле нет — предъявить его будет нечем")
	}

	// Права файла: это единственная защита содержимого, ровно как у ключей провайдеров.
	fi, err := os.Stat(s.machineTokensPath())
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("права файла %v, ожидались 0600", fi.Mode().Perm())
	}

	// И список наружу не несёт ни хеша, ни значения.
	list, ok := s.listMachineTokens()
	if !ok || len(list) != 1 {
		t.Fatalf("список: ok=%v len=%d", ok, len(list))
	}
	blob, _ := json.Marshal(list)
	for _, forbidden := range []string{value, rec.Hash} {
		if strings.Contains(string(blob), forbidden) {
			t.Fatalf("список отдал секрет: %s", blob)
		}
	}
}

// ⚠ ТРИ СВОЙСТВА ОТЗЫВА В ОДНОМ СЦЕНАРИИ, ПОТОМУ ЧТО ОНИ ДРУГ БЕЗ ДРУГА НИЧЕГО НЕ ЗНАЧАТ.
func TestRevokingOneTokenLeavesTheOthersWorking(t *testing.T) {
	s := mtFixture(t)
	a, valA, err := s.issueMachineToken("ci")
	if err != nil {
		t.Fatal(err)
	}
	_, valB, err := s.issueMachineToken("laptop")
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := s.machineTokenMatches(valA); !ok {
		t.Fatal("свежевыданный токен не принимается")
	}
	if _, ok := s.machineTokenMatches(valB); !ok {
		t.Fatal("второй свежевыданный токен не принимается")
	}

	if _, found, err := s.revokeMachineToken(a.ID); err != nil || !found {
		t.Fatalf("отзыв: found=%v err=%v", found, err)
	}
	if _, ok := s.machineTokenMatches(valA); ok {
		t.Fatal("ОТОЗВАННЫЙ токен всё ещё пускает — отзыв не отозвал")
	}
	if _, ok := s.machineTokenMatches(valB); !ok {
		t.Fatal("отзыв одного токена сломал ДРУГОЙ — ради чего им и давали имена")
	}
	// Серверный токен живёт своей жизнью и отзывом именованных не затрагивается.
	if c, ok := s.resolveCred("server-machine-token-long"); !ok || !c.machine {
		t.Fatalf("серверный токен перестал работать: ok=%v caller=%+v", ok, c)
	}
}

// Отзыв того, чего нет, обязан СКАЗАТЬ об этом. «Отозвано» над живым доступом отправляет
// администратора считать дверь закрытой — то же, за что заведена [DELETE-USER-CANNOT-FAIL-OUT-LOUD].
func TestRevokingAnAbsentTokenSaysSo(t *testing.T) {
	s := mtFixture(t)
	if _, found, err := s.revokeMachineToken("no-such-id"); err != nil || found {
		t.Fatalf("отзыв несуществующего: found=%v err=%v — ожидалось found=false без ошибки", found, err)
	}
}

func TestMachineTokenGuardsItsInputs(t *testing.T) {
	s := mtFixture(t)
	// Хранилище отвергает безымянный токен САМО: правило принадлежит ему, а обработчик — лишь один
	// из вызывателей. Первая редакция проверяла только валидатор, и функция принимала пустое имя.
	if _, _, err := s.issueMachineToken(""); err == nil {
		t.Fatal("issueMachineToken принял пустое имя — правило живёт не там, где предмет")
	}
	if msg := validMachineTokenName("  "); msg == "" {
		t.Fatal("пустое имя принято — безымянный токен невозможно отозвать осмысленно")
	}
	if msg := validMachineTokenName(strings.Repeat("x", machineTokenNameMax+1)); msg == "" {
		t.Fatal("слишком длинное имя принято")
	}
	if msg := validMachineTokenName("ci\nfake"); msg == "" {
		t.Fatal("имя с управляющим символом принято — оно попадёт в журнал и в список")
	}
	if msg := validMachineTokenName("ci"); msg != "" {
		t.Fatalf("нормальное имя отвергнуто: %s", msg)
	}
	if _, _, err := s.issueMachineToken("ci"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := s.issueMachineToken("CI"); err == nil {
		t.Fatal("имя, отличающееся только регистром, принято — два токена станут неразличимы в списке")
	}
}

// Повреждённый файл обязан сообщаться КАК повреждённый, а не как «токенов нет»: второе зовёт
// записать поверх и потерять то, что сервер не смог разобрать.
func TestUnreadableMachineTokenFileIsReportedNotSwallowed(t *testing.T) {
	s := mtFixture(t)
	if err := os.MkdirAll(filepath.Dir(s.machineTokensPath()), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(s.machineTokensPath(), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, ok := s.readMachineTokens(); ok {
		t.Fatal("повреждённый файл прочитан как исправный")
	}
	if _, ok := s.listMachineTokens(); ok {
		t.Fatal("список над повреждённым файлом отдал «пусто» вместо отказа")
	}
	// И предъявленный токен над повреждённым файлом НЕ принимается: fail-closed.
	if _, ok := s.machineTokenMatches("whatever"); ok {
		t.Fatal("над повреждённым файлом токен был принят")
	}
	if _, _, err := s.issueMachineToken("ci"); err == nil {
		t.Fatal("выдача поверх повреждённого файла затёрла бы его")
	}
}

/* ------------------------------------------------------------------ пол операторского токена */

// ⚠ ЗАМЕР, КУПИВШИЙ ЭТУ ПРОВЕРКУ: ветка окружения возвращала значение ДОСЛОВНО и до всякой проверки,
// тогда как `tokenMinLen` стоял только на ветке чтения файла. `CONTROL_API_TOKEN=x` принимался.
func TestShortEnvTokenIsRefusedNotAccepted(t *testing.T) {
	t.Setenv("CONTROL_API_TOKEN", "x")
	tok, src, _, warnings := resolveToken(t.TempDir())
	if src != tokenRefused {
		t.Fatalf("источник %q, ожидался %q — короткий токен принят", src, tokenRefused)
	}
	if tok != "" {
		t.Fatal("отказ вернул непустой токен")
	}
	if len(warnings) == 0 {
		t.Fatal("отказ молчит — оператор не узнает, почему сервер не поднялся")
	}
	// Сообщение обязано НАЗЫВАТЬ переменную: «unusable token» без имени не говорит, что править.
	if !strings.Contains(warnings[0], "CONTROL_API_TOKEN") {
		t.Fatalf("причина не называет переменную: %q", warnings[0])
	}
}

func TestLongEnvTokenStillWins(t *testing.T) {
	t.Setenv("CONTROL_API_TOKEN", "a-perfectly-long-token")
	tok, src, _, _ := resolveToken(t.TempDir())
	if src != tokenFromEnv || tok != "a-perfectly-long-token" {
		t.Fatalf("источник %q токен %q — пригодный операторский токен обязан выигрывать как прежде", src, tok)
	}
}
