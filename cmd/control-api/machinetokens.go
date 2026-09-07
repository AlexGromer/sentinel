// Именованные отзываемые машинные токены (ADR-160).
//
// ЧТО БЫЛО, ЗАМЕРЕНО: `resolveToken` даёт РОВНО ОДИН токен на развёртывание — из
// `CONTROL_API_TOKEN`, из файла `state/control-api.token` или сгенерированный. Следствия, каждое из
// которых стоит отдельно: выдать CI собственный токен нельзя · отозвать «только у CI», не сломав
// всех остальных, нельзя · узнать, КТО из машин им пользуется, нельзя · и файл хранит секрет
// ОТКРЫТЫМ, потому что он же и предъявляется.
//
// ⚠ ЗНАЧЕНИЕ ГЕНЕРИРУЕТ ТОЛЬКО СЕРВЕР (решение Alex 2026-09-07), и это снимает вопрос, а не
// откладывает его. У сгенерированных 32 байт `crypto/rand` — 256 бит, перебор невозможен, поэтому
// хранить достаточно SHA-256. Медленный KDF здесь был бы ошибкой того же класса, что «сильное
// средство не к той задаче»: он ничего не добавляет к неперебираемому секрету, а его задержка легла
// бы на КАЖДЫЙ запрос CI. Разрешить оператору принести своё значение значило бы завести второй
// режим (слабый секрет → нужен KDF либо отказ) ради удобства, которого никто не просил.
//
// ⚠ ФАЙЛ 0600, А НЕ ХРАНИЛИЩЕ, И ПОСЛЕ ADR-158 ЭТО НАДО ОБОСНОВАТЬ ЗАНОВО. ADR-146 держит
// кредентиалы вне store-gateway, потому что его сокет доступен любому процессу того же UID,
// предъявившему `STORE_TOKEN`. ADR-158 сделал хранилище присутствующим ВСЕГДА, и на встроенном
// ярусе сокета нет вовсе — но на ярусе с внешним шлюзом он есть, а токены обязаны вести себя
// ОДИНАКОВО на обоих (директива «одна версия»). Из двух носителей одинаково доступен только файл,
// и он же строго теснее. Поэтому — файл, тот же приём и та же атомарная запись, что у ключей
// провайдеров.
//
// ⚠ ЗАПИСЬ ПОД МЬЮТЕКСОМ, В ОТЛИЧИЕ ОТ `providerkeys.go`. Там пара «прочитать документ — изменить —
// записать целиком» идёт без блокировки, и два одновременных PUT теряют один ключ (заведено как
// `[PROVIDER-KEYS-WRITE-IS-A-READ-MODIFY-WRITE-RACE]`). Здесь цена та же и предмет тот же, поэтому
// повторять форму без её дефекта.
package main

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	machineTokensFileName = "machine-tokens.json"
	machineTokensVersion  = 1
	machineTokenBytes     = 32 // → 64 hex-знаков, 256 бит
	machineTokenNameMax   = 64
	machineTokensMax      = 128 // потолок на число выданных: файл читается на КАЖДОМ запросе
)

// machineTokensMu сериализует пару чтение-запись. Отдельный мьютекс, а не `s.mu`: тот стережёт
// прогоны в памяти и берётся на горячем пути, а здесь речь о файле, который трогают редко.
var machineTokensMu sync.Mutex

// storedMachineToken — то, что лежит на диске. Значения токена здесь НЕТ и быть не может: хранится
// только SHA-256 от него. `Hint` — последние четыре знака, ровно как у ключей провайдеров: этого
// хватает отличить «тот токен, который я имел в виду» от «какой-то другой» и не хватает ни для чего
// ещё.
type storedMachineToken struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Hash      string `json:"hash"`
	Hint      string `json:"hint"`
	CreatedAt string `json:"created_at"`
}

type machineTokensDoc struct {
	Version int                  `json:"version"`
	Tokens  []storedMachineToken `json:"tokens"`
}

func (s *server) machineTokensPath() string {
	return filepath.Join(s.repo, "state", machineTokensFileName)
}

// hashMachineToken — SHA-256 в hex. Вынесено в функцию, потому что у неё ДВА вызывателя (выдача и
// проверка), и разойтись им нельзя: разошедшись, они дали бы токен, который невозможно предъявить.
func hashMachineToken(tok string) string {
	sum := sha256.Sum256([]byte(tok))
	return hex.EncodeToString(sum[:])
}

// machineTokenHint — последние четыре знака. Короткое значение не получает подсказки вовсе, но у
// сгенерированных длина фиксирована, так что ветка существует только на случай чужого файла.
func machineTokenHint(tok string) string {
	if len(tok) < 12 {
		return ""
	}
	return tok[len(tok)-4:]
}

// readMachineTokens: отсутствующий файл — это ПУСТОЙ документ, а не ошибка (свежее развёртывание не
// выдало ни одного токена). Нечитаемый — сообщается через bool, чтобы администратору можно было
// сказать «файл повреждён», а не «токенов нет»: второе читается как «ничего не выдавали» и зовёт
// записать поверх того, что сервер не смог разобрать.
func (s *server) readMachineTokens() (machineTokensDoc, bool) {
	empty := machineTokensDoc{Version: machineTokensVersion}
	b, err := os.ReadFile(s.machineTokensPath())
	if err != nil {
		return empty, true
	}
	var doc machineTokensDoc
	if json.Unmarshal(b, &doc) != nil {
		return empty, false
	}
	if doc.Version == 0 {
		doc.Version = machineTokensVersion
	}
	return doc, true
}

// writeMachineTokens пишет атомарно во временный файл 0600 и переименовывает — как `writeConfigFile`
// и `writeProviderKeys`. Наполовину записанный файл здесь означал бы, что ВСЕ выданные токены
// перестали приниматься разом.
func (s *server) writeMachineTokens(doc machineTokensDoc) error {
	path := s.machineTokensPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	doc.Version = machineTokensVersion
	enc, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".machine-tokens-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	// Chmod ДО записи: файл ни одного мгновения не должен существовать шире, чем 0600.
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(enc); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// machineTokenMatches сверяет предъявленный токен с выданными и возвращает ИМЯ совпавшего.
//
// ⚠ СРАВНЕНИЕ ПОСТОЯННОГО ВРЕМЕНИ, И ЦИКЛ НЕ ПРЕРЫВАЕТСЯ НА СОВПАДЕНИИ. Хеши не секрет сами по
// себе, но ранний выход делает длительность ответа функцией ПОЗИЦИИ токена в файле, а порядок
// позиций — это порядок выдачи. Проходим весь список всегда.
func (s *server) machineTokenMatches(tok string) (string, bool) {
	if tok == "" {
		return "", false
	}
	doc, ok := s.readMachineTokens()
	if !ok {
		return "", false
	}
	want := []byte(hashMachineToken(tok))
	name, found := "", false
	for _, t := range doc.Tokens {
		if subtle.ConstantTimeCompare(want, []byte(t.Hash)) == 1 {
			name, found = t.Name, true
		}
	}
	return name, found
}

// validMachineTokenName: имя показывается в списке и в журнале, поэтому оно обязано быть коротким и
// печатным. Пустое отвергается — безымянный токен возвращает ровно ту задачу, ради которой этот
// файл написан («узнать, кто из машин им пользуется, нельзя»).
func validMachineTokenName(name string) string {
	n := strings.TrimSpace(name)
	switch {
	case n == "":
		return "a machine token needs a name — an unnamed one cannot be told from another when the time comes to revoke it"
	case len(n) > machineTokenNameMax:
		return fmt.Sprintf("the name must be at most %d characters", machineTokenNameMax)
	}
	for _, r := range n {
		if r < ' ' || r == 0x7f {
			return "the name must not contain control characters"
		}
	}
	return ""
}

func newMachineTokenValue() (string, error) {
	b := make([]byte, machineTokenBytes)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// issueMachineToken заводит токен и возвращает его значение — ЕДИНСТВЕННЫЙ раз за его жизнь.
func (s *server) issueMachineToken(name string) (storedMachineToken, string, error) {
	machineTokensMu.Lock()
	defer machineTokensMu.Unlock()

	// ⚠ ИМЯ ПРОВЕРЯЕТСЯ ЗДЕСЬ, А НЕ ТОЛЬКО В ОБРАБОТЧИКЕ. Правило «безымянных токенов не бывает»
	// принадлежит хранилищу: обработчик — лишь один из его вызывателей, и второй (глагол CLI, тест,
	// будущий импорт) обошёл бы проверку молча. Гейт поймал это ровно так — вызвав функцию напрямую.
	if msg := validMachineTokenName(name); msg != "" {
		return storedMachineToken{}, "", fmt.Errorf("%s", msg)
	}
	name = strings.TrimSpace(name)
	doc, readable := s.readMachineTokens()
	if !readable {
		return storedMachineToken{}, "", fmt.Errorf("the stored machine-token file is unreadable; fix or remove %s", s.machineTokensPath())
	}
	for _, t := range doc.Tokens {
		if strings.EqualFold(t.Name, name) {
			return storedMachineToken{}, "", fmt.Errorf("a machine token named %q already exists; revoke it first", name)
		}
	}
	if len(doc.Tokens) >= machineTokensMax {
		return storedMachineToken{}, "", fmt.Errorf("this deployment already has %d machine tokens (the maximum); revoke one first", len(doc.Tokens))
	}
	val, err := newMachineTokenValue()
	if err != nil {
		return storedMachineToken{}, "", err
	}
	rec := storedMachineToken{
		ID:        newRunID(),
		Name:      name,
		Hash:      hashMachineToken(val),
		Hint:      machineTokenHint(val),
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	doc.Tokens = append(doc.Tokens, rec)
	if err := s.writeMachineTokens(doc); err != nil {
		return storedMachineToken{}, "", err
	}
	return rec, val, nil
}

// revokeMachineToken удаляет запись и ГОВОРИТ, нашлась ли она. В отличие от `deleteUser`, который
// ничего не возвращает и потому не умеет отказать вслух (заведено как
// `[DELETE-USER-CANNOT-FAIL-OUT-LOUD]`), здесь «отозвано» над живым токеном было бы опаснее всего:
// администратор ушёл бы уверенным, что доступ закрыт.
func (s *server) revokeMachineToken(id string) (storedMachineToken, bool, error) {
	machineTokensMu.Lock()
	defer machineTokensMu.Unlock()

	doc, readable := s.readMachineTokens()
	if !readable {
		return storedMachineToken{}, false, fmt.Errorf("the stored machine-token file is unreadable; fix or remove %s", s.machineTokensPath())
	}
	kept := doc.Tokens[:0:0]
	var removed storedMachineToken
	found := false
	for _, t := range doc.Tokens {
		if t.ID == id {
			removed, found = t, true
			continue
		}
		kept = append(kept, t)
	}
	if !found {
		return storedMachineToken{}, false, nil
	}
	doc.Tokens = kept
	if err := s.writeMachineTokens(doc); err != nil {
		return storedMachineToken{}, false, err
	}
	return removed, true, nil
}

// listMachineTokens отдаёт записи БЕЗ хешей, отсортированные по времени выдачи. Хеш наружу не идёт
// не потому, что он секрет, а потому, что у него нет ни одного применения на стороне читателя:
// показать его значило бы предложить сверять токены вручную.
func (s *server) listMachineTokens() ([]map[string]any, bool) {
	doc, ok := s.readMachineTokens()
	if !ok {
		return nil, false
	}
	sort.SliceStable(doc.Tokens, func(i, j int) bool { return doc.Tokens[i].CreatedAt < doc.Tokens[j].CreatedAt })
	out := make([]map[string]any, 0, len(doc.Tokens))
	for _, t := range doc.Tokens {
		out = append(out, map[string]any{
			"id": t.ID, "name": t.Name, "hint": t.Hint, "created_at": t.CreatedAt,
		})
	}
	return out, true
}

/* ------------------------------------------------------------------ HTTP */

type issueMachineTokenReq struct {
	Name string `json:"name"`
}

// handleListMachineTokens: что выдано, кому и когда — но НИКОГДА значения.
func (s *server) handleListMachineTokens(w http.ResponseWriter, r *http.Request) {
	list, ok := s.listMachineTokens()
	if !ok {
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "the stored machine-token file is unreadable; fix or remove " + s.machineTokensPath()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"tokens": list, "total": len(list)})
}

// handleIssueMachineToken выдаёт токен и отдаёт его значение ЕДИНСТВЕННЫЙ раз.
//
// ⚠ ЗНАЧЕНИЕ В ОТВЕТЕ — ЭТО ВЕСЬ СМЫСЛ ОДНОРАЗОВОСТИ, а не послабление. Хранится только SHA-256,
// поэтому сервер физически не может показать его второй раз; ответ — единственный момент, когда оно
// существует за пределами вызывающего. Из этого следует и то, чего здесь НЕТ: значение не пишется в
// журнал сервиса (он читается через интерфейс и переживает перезапуск) и не возвращается списком.
func (s *server) handleIssueMachineToken(w http.ResponseWriter, r *http.Request) {
	var req issueMachineTokenReq
	if json.NewDecoder(r.Body).Decode(&req) != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "malformed JSON body"})
		return
	}
	name := strings.TrimSpace(req.Name)
	if msg := validMachineTokenName(name); msg != "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": msg})
		return
	}
	rec, value, err := s.issueMachineToken(name)
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}
	s.journalEvent("service.machine_token_issued", "info", map[string]string{"actor": rec.Name}, nil)
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": rec.ID, "name": rec.Name, "hint": rec.Hint, "created_at": rec.CreatedAt,
		"token": value,
		"note":  "this value is shown ONCE — the server stores only its hash and cannot show it again",
	})
}

// handleRevokeMachineToken отзывает токен и ГОВОРИТ, был ли он.
func (s *server) handleRevokeMachineToken(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "which token? the path needs an id"})
		return
	}
	rec, found, err := s.revokeMachineToken(id)
	if err != nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}
	if !found {
		// 404, а не 200: «отозвано» над тем, чего не было, отправляет администратора считать доступ
		// закрытым. Ровно та ошибка, из-за которой заведена [DELETE-USER-CANNOT-FAIL-OUT-LOUD].
		writeJSON(w, http.StatusNotFound, map[string]string{
			"error": "no machine token with id " + id + " — nothing was revoked"})
		return
	}
	s.journalEvent("service.machine_token_revoked", "info", map[string]string{"actor": rec.Name}, nil)
	writeJSON(w, http.StatusOK, map[string]any{"status": "revoked", "id": rec.ID, "name": rec.Name})
}
