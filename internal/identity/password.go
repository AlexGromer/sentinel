// Package identity holds the password KDF for ADR-109 local accounts.
//
// Argon2id (RFC 9106) for NEW hashes; PBKDF2-HMAC-SHA256 (RFC 8018 §5.2) stays as a VERIFIER so no
// existing password is invalidated. See ADR-161.
//
// ⚠ ЧТО ЗДЕСЬ БЫЛО НАПИСАНО И ОКАЗАЛОСЬ НЕВЕРНЫМ, ЗАМЕРЕНО 2026-09-07. Прежний заголовок объяснял
// отказ от `x/crypto` тем, что тот тянет транзитивный `golang.org/x/net`, которого нет в кеше
// сборки. Три поправки, каждая замерена:
//
//	1. Оговорка была про BCRYPT, а не про Argon2 — и к argon2 не относится вовсе: `go list -deps
//	   golang.org/x/crypto/argon2` даёт `blake2b` и `x/sys/cpu`, и НИ ОДНОГО `x/net`.
//	2. `x/net` уже в `go.mod` (v0.55.0, indirect) и добавление `x/crypto` его НЕ СДВИНУЛО:
//	   `go.mod` вырос ровно на одну строку.
//	3. Air-gap в этом проекте — про РАНТАЙМ, а не про сборку: `Dockerfile:9` делает
//	   `RUN go mod download`, то есть сборка ходит в сеть по построению, а `vendor/` в дереве нет.
//
// Настоящая цена оказалась одна и разовая: `x/crypto@v0.51.0` (версия, которую выбирает MVS) не был
// распакован в локальном кеше, и его пришлось скачать один раз.
//
// ПАРАМЕТРЫ ВЫБРАНЫ ЗАМЕРОМ, А НЕ ПО ВКУСУ (4 ядра, эта машина):
//
//	PBKDF2 600k                 432 мс   ← что было
//	argon2id t=3 m=64MiB p=4     84 мс   ← RFC 9106, второй рекомендованный
//	argon2id t=3 m=64MiB p=1    182 мс   ← выбрано
//
// То есть переход СНИЖАЕТ стоимость входа более чем вдвое и одновременно делает подбор
// памяти-жёстким. `p=1`, а не 4, намеренно: параллелизм ставит стоимость ЗАЩИТНИКА в зависимость от
// числа ядер машины, тогда как у нападающего их всегда больше — предсказуемость здесь стоит дороже
// тех 98 мс, а нижняя граница OWASP (m=19MiB, t=2) перекрыта втрое по памяти.
//
// Ни один KDF не доверяется на глаз. TestPBKDF2MatchesReference сверяет реализацию PBKDF2 с
// `hashlib.pbkdf2_hmac` питона — отдельной, независимо написанной реализацией, которая приезжает с
// интерпретатором, на котором и так работает brain. Векторы ВЫВОДЯТСЯ прогоном, а не набираются по
// памяти: константа, вспомненная вместо вычисленной, совпала бы с неверной реализацией ровно тогда,
// когда это важно.
//
// ⚠ У ARGON2ID ТАКОЙ СВЕРКИ ЗДЕСЬ НЕТ, И ЭТО ОБЪЯВЛЕНО, А НЕ УМОЛЧАНО. Причина замерена: второй
// независимой реализации в этом окружении не существует — `argon2` нет ни в питоне venv, ни в
// зависимостях проекта, а поставить его нечем (в venv нет и pip). Вписать сюда вектор из RFC 9106 по
// памяти было бы ХУЖЕ отсутствия сверки: это ровно та «константа, вспомненная вместо вычисленной»,
// против которой написан абзац выше, и совпала бы она с неверной реализацией именно тогда, когда это
// важно. Поэтому Argon2id проверяется СВОЙСТВАМИ, которые не требуют эталона: круговой ход, соль
// разная на каждый вызов, параметры едут с хешем и читаются обратно, чувствительность к КАЖДОМУ
// параметру по отдельности, отказ на испорченном. Чем это слабее сверки — тем, что согласованно
// неверная реализация прошла бы; поднимать это до эталона следует, как только независимая
// реализация окажется доступна.
package identity

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"strconv"
	"strings"

	"golang.org/x/crypto/argon2"
)

// Iterations for a NEW hash. Stored per-hash (see Hash's encoding) so raising this number does not
// invalidate existing passwords — an old hash keeps verifying with the count it was made under, and
// gets the new one whenever it is next set.
const DefaultIterations = 600_000

const (
	saltBytes = 16
	keyBytes  = 32
	scheme    = "pbkdf2-sha256"
	// schemeArgon2id именует НОВЫЙ формат. Имя схемы едет вместе с хешем по той же причине, что и
	// счётчик итераций у PBKDF2: проверяющий, знающий только сегодняшний алгоритм, не смог бы
	// проверить вчерашний хеш — а это и есть «смена политики выкинула всех наружу».
	schemeArgon2id = "argon2id"
)

// Параметры Argon2id для НОВОГО хеша. Выбраны замером (см. заголовок пакета): 182 мс на этой машине
// против 432 мс у PBKDF2, память-жёстко, p=1 ради предсказуемости на чужом железе.
//
// Каждый параметр едет В САМОМ ХЕШЕ, поэтому поднять любой из них можно, не тронув ни одного
// существующего пароля: старый проверяется своими значениями и переписывается при первом же входе.
const (
	argonTime    uint32 = 3
	argonMemory  uint32 = 64 * 1024 // KiB
	argonThreads uint8  = 1
)

// pbkdf2 is RFC 8018 §5.2 over HMAC-SHA256, specialised to one output block because keyBytes (32) is
// exactly the hash size. Written as the general loop anyway, so a longer key later is not a rewrite.
func pbkdf2(password, salt []byte, iter, keyLen int) []byte {
	prf := hmac.New(sha256.New, password)
	hLen := prf.Size()
	blocks := (keyLen + hLen - 1) / hLen
	out := make([]byte, 0, blocks*hLen)
	var counter [4]byte
	for block := 1; block <= blocks; block++ {
		binary.BigEndian.PutUint32(counter[:], uint32(block))
		prf.Reset()
		prf.Write(salt)
		prf.Write(counter[:])
		u := prf.Sum(nil)
		t := make([]byte, len(u))
		copy(t, u)
		for i := 1; i < iter; i++ {
			prf.Reset()
			prf.Write(u)
			u = prf.Sum(u[:0])
			for j := range t {
				t[j] ^= u[j]
			}
		}
		out = append(out, t...)
	}
	return out[:keyLen]
}

// Hash derives a storable credential: "argon2id$m=<KiB>,t=<passes>,p=<lanes>$<salt-b64>$<key-b64>".
//
// The scheme name and every parameter travel WITH the hash rather than living in a constant the
// verifier reads. A verifier that assumes today's parameters cannot check a hash made under
// yesterday's, which is how a policy change locks every existing user out — and it is exactly what
// made the move off PBKDF2 free (ADR-161).
func Hash(password string) (string, error) {
	if password == "" {
		return "", fmt.Errorf("identity: refusing to hash an empty password")
	}
	salt := make([]byte, saltBytes)
	if _, err := rand.Read(salt); err != nil {
		// Fail closed. A predictable salt would make every hash in the table attackable together, and
		// a degraded credential is worse than a refused one.
		return "", fmt.Errorf("identity: salt: %w", err)
	}
	key := argon2.IDKey([]byte(password), salt, argonTime, argonMemory, argonThreads, keyBytes)
	enc := base64.RawStdEncoding
	// Четыре части, разделённые `$`, — та же ФОРМА, что у PBKDF2, чтобы у обоих форматов был один
	// разбор и одна точка отказа. Параметры лежат во второй части как `m=…,t=…,p=…`.
	return fmt.Sprintf("%s$m=%d,t=%d,p=%d$%s$%s", schemeArgon2id, argonMemory, argonTime, argonThreads,
		enc.EncodeToString(salt), enc.EncodeToString(key)), nil
}

// parseArgonParams разбирает вторую часть хеша argon2id. Возвращает false на всём, чего не понимает:
// хеш, который не разобрать, — это не пароль, который совпал.
func parseArgonParams(s string) (mem, time uint32, par uint8, ok bool) {
	var m, t, p uint64
	seen := 0
	for _, kv := range strings.Split(s, ",") {
		k, v, found := strings.Cut(kv, "=")
		if !found {
			return 0, 0, 0, false
		}
		n, err := strconv.ParseUint(v, 10, 32)
		if err != nil || n == 0 {
			return 0, 0, 0, false
		}
		switch k {
		case "m":
			m, seen = n, seen|1
		case "t":
			t, seen = n, seen|2
		case "p":
			p, seen = n, seen|4
		default:
			return 0, 0, 0, false
		}
	}
	if seen != 7 || p > 255 {
		return 0, 0, 0, false
	}
	return uint32(m), uint32(t), uint8(p), true
}

// Verify reports whether password produced stored. False for anything malformed — a hash this code
// cannot parse is not a password that matches.
//
// ⚠ ДИСПЕТЧЕР по имени схемы, и обе ветки обязаны остаться: PBKDF2 больше не производит новых
// хешей, но продолжает проверять уже существующие. Выкинуть его значило бы выкинуть всех, кто завёл
// пароль до ADR-161, — миграция, оплаченная сбросом паролей, тогда как эта не стоит ничего.
func Verify(stored, password string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 4 {
		return false
	}
	enc := base64.RawStdEncoding
	salt, err1 := enc.DecodeString(parts[2])
	want, err2 := enc.DecodeString(parts[3])
	if err1 != nil || err2 != nil || len(want) == 0 {
		return false
	}
	var got []byte
	switch parts[0] {
	case schemeArgon2id:
		mem, tm, par, ok := parseArgonParams(parts[1])
		if !ok {
			return false
		}
		got = argon2.IDKey([]byte(password), salt, tm, mem, par, uint32(len(want)))
	case scheme:
		iter, err := strconv.Atoi(parts[1])
		if err != nil || iter < 1 {
			return false
		}
		got = pbkdf2([]byte(password), salt, iter, len(want))
	default:
		return false
	}
	// Constant-time, matching how the machine bearer is compared (cmd/control-api/main.go): a timing
	// difference on a credential check is a credential leak.
	return subtle.ConstantTimeCompare(got, want) == 1
}

// NeedsRehash reports whether stored was made under weaker parameters than the current policy, so a
// successful login can quietly upgrade it.
func NeedsRehash(stored string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 4 {
		return true
	}
	switch parts[0] {
	case schemeArgon2id:
		mem, tm, par, ok := parseArgonParams(parts[1])
		// Слабее ЛЮБОГО из сегодняшних параметров — значит переписать. Сравнение поштучное, а не по
		// какой-нибудь «суммарной стоимости»: поднять память, уронив время, — это ослабление, которое
		// одно число спрятало бы.
		return !ok || mem < argonMemory || tm < argonTime || par < argonThreads
	case scheme:
		// PBKDF2 теперь ВСЕГДА устарел, каким бы ни был счётчик: схема сменилась, а не параметр.
		return true
	default:
		return true
	}
}

// DummyHash returns a well-formed hash of the CURRENT scheme and parameters, made from a fixed salt
// and a fixed key that no password can produce.
//
// ⚠ ОНО ЖИВЁТ ЗДЕСЬ, А НЕ У ВЫЗЫВАТЕЛЯ, И ЭТО НЕ АККУРАТНОСТЬ, А ИСПРАВЛЕНИЕ ДЕФЕКТА. Вход
// прогоняет KDF и на несуществующем имени — иначе «нет такого имени» отличалось бы от «неверный
// пароль» по ВРЕМЕНИ, то есть маршрут превращался бы в список заведённых аккаунтов. Пока фиктивный
// хеш собирался в `session.go` строкой `"pbkdf2-sha256$"+…`, он был привязан к схеме, которую здесь
// сменили: реальная проверка стала стоить 182 мс, а фиктивная осталась на 432 мс, и канал по времени
// открылся бы ЗАНОВО, только наоборот — несуществующее имя дороже существующего. Формат принадлежит
// этому пакету, значит и его двойник тоже.
func DummyHash() string {
	enc := base64.RawStdEncoding
	return fmt.Sprintf("%s$m=%d,t=%d,p=%d$%s$%s", schemeArgon2id, argonMemory, argonTime, argonThreads,
		enc.EncodeToString(make([]byte, saltBytes)), enc.EncodeToString(make([]byte, keyBytes)))
}
