package identity

import (
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"golang.org/x/crypto/argon2"
	"os/exec"
	"strings"
	"testing"
)

// TestPBKDF2MatchesReference checks this PBKDF2 against Python's hashlib.pbkdf2_hmac — a separate
// implementation, written by other people, that ships with the interpreter the brain already runs on.
//
// The vectors are DERIVED by running it, not typed in. A constant recalled from memory is exactly the
// kind of "expected value" that agrees with a wrong implementation: it would be produced by the same
// reasoning that produced the code, and would confirm it. Asking a second implementation cannot.
//
// Skipped rather than failed when no python3 is on PATH: this asserts agreement between two
// implementations, and with only one present there is nothing to compare.
func TestPBKDF2MatchesReference(t *testing.T) {
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not on PATH — no reference implementation to compare against")
	}
	cases := []struct {
		pw, salt    string
		iter, dklen int
	}{
		{"password", "salt", 1, 32},
		{"password", "salt", 2, 32},
		{"password", "salt", 4096, 32},
		{"passwordPASSWORDpassword", "saltSALTsaltSALTsaltSALTsaltSALTsalt", 4096, 40}, // >1 block
		{"pass\x00word", "sa\x00lt", 4096, 16},                                         // embedded NULs
		{"пароль-с-юникодом", "соль", 1000, 32},                                        // non-ASCII
	}
	// One python3 invocation for every case: a per-case process would make 600k-iteration cases
	// unbearable and invites the temptation to shrink the test instead.
	script := `
import hashlib, json, sys
out = []
for c in json.load(sys.stdin):
    dk = hashlib.pbkdf2_hmac('sha256', c['pw'].encode('utf-8', 'surrogateescape'),
                             c['salt'].encode('utf-8', 'surrogateescape'), c['iter'], c['dklen'])
    out.append(dk.hex())
print(json.dumps(out))
`
	in, err := json.Marshal(cases2json(cases))
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("python3", "-c", script)
	cmd.Stdin = strings.NewReader(string(in))
	raw, err := cmd.Output()
	if err != nil {
		t.Fatalf("reference implementation failed to run: %v", err)
	}
	var want []string
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatalf("reference output: %v (%s)", err, raw)
	}
	if len(want) != len(cases) {
		t.Fatalf("reference returned %d vectors for %d cases", len(want), len(cases))
	}
	for i, c := range cases {
		got := hex.EncodeToString(pbkdf2([]byte(c.pw), []byte(c.salt), c.iter, c.dklen))
		if got != want[i] {
			t.Errorf("case %d (pw=%q salt=%q iter=%d len=%d):\n  ours: %s\n  python: %s",
				i, c.pw, c.salt, c.iter, c.dklen, got, want[i])
		}
	}
}

type refCase struct {
	PW    string `json:"pw"`
	Salt  string `json:"salt"`
	Iter  int    `json:"iter"`
	DKLen int    `json:"dklen"`
}

func cases2json(cs []struct {
	pw, salt    string
	iter, dklen int
}) []refCase {
	out := make([]refCase, 0, len(cs))
	for _, c := range cs {
		out = append(out, refCase{c.pw, c.salt, c.iter, c.dklen})
	}
	return out
}

func TestHashVerifyRoundTrip(t *testing.T) {
	h, err := Hash("correct horse battery staple")
	if err != nil {
		t.Fatal(err)
	}
	if !Verify(h, "correct horse battery staple") {
		t.Error("the password that made the hash does not verify against it")
	}
	if Verify(h, "correct horse battery stapl") {
		t.Error("a near-miss password verified")
	}
	if Verify(h, "") {
		t.Error("an empty password verified")
	}
}

// TestHashIsSaltedPerCall: two hashes of the SAME password must differ, or the table tells an attacker
// which accounts share a password and lets one cracked hash unlock all of them.
func TestHashIsSaltedPerCall(t *testing.T) {
	a, err1 := Hash("same")
	b, err2 := Hash("same")
	if err1 != nil || err2 != nil {
		t.Fatal(err1, err2)
	}
	if a == b {
		t.Fatal("two hashes of one password are identical — the salt is not per-call")
	}
	if !Verify(a, "same") || !Verify(b, "same") {
		t.Error("a salted hash stopped verifying")
	}
}

// TestVerifyRejectsMalformed: every shape that is not a hash this code wrote. A parser that guesses is
// a parser that can be talked into agreeing.
func TestVerifyRejectsMalformed(t *testing.T) {
	good, _ := Hash("pw")
	parts := strings.Split(good, "$")
	for name, stored := range map[string]string{
		"empty":           "",
		"no separators":   "pbkdf2-sha256",
		"wrong scheme":    "md5$1$" + parts[2] + "$" + parts[3],
		"iterations zero": "pbkdf2-sha256$0$" + parts[2] + "$" + parts[3],
		"iterations text": "pbkdf2-sha256$many$" + parts[2] + "$" + parts[3],
		"bad base64 salt": "pbkdf2-sha256$1$!!!$" + parts[3],
		"bad base64 key":  "pbkdf2-sha256$1$" + parts[2] + "$!!!",
		"empty key":       "pbkdf2-sha256$1$" + parts[2] + "$",
		"extra field":     good + "$extra",
		"plaintext":       "pw",
	} {
		if Verify(stored, "pw") {
			t.Errorf("%s: a malformed credential verified", name)
		}
	}
}

// TestHashRefusesEmptyPassword: an empty password must not become a storable credential, because
// Verify("", …) would then be a valid login for anyone who sends nothing.
func TestHashRefusesEmptyPassword(t *testing.T) {
	if _, err := Hash(""); err == nil {
		t.Fatal("Hash accepted an empty password")
	}
}

// TestParametersTravelWithTheHash: a hash made under a LOWER iteration count still verifies, and is
// reported as needing a rehash. A verifier that assumed the current constant would lock out every
// existing user the moment the policy was raised.
func TestParametersTravelWithTheHash(t *testing.T) {
	enc := base64.RawStdEncoding
	salt := []byte("0123456789abcdef")
	old := "pbkdf2-sha256$1000$" + enc.EncodeToString(salt) + "$" +
		enc.EncodeToString(pbkdf2([]byte("pw"), salt, 1000, keyBytes))
	if !Verify(old, "pw") {
		t.Error("a hash made under fewer iterations stopped verifying")
	}
	if !NeedsRehash(old) {
		t.Error("a weaker hash was not flagged for rehash")
	}
	fresh, _ := Hash("pw")
	if NeedsRehash(fresh) {
		t.Error("a hash at the current policy was flagged for rehash")
	}
}

/* ------------------------------------------------------------------ Argon2id (ADR-161) */

// ⚠ ЭТИ ПРОВЕРКИ — СВОЙСТВА, А НЕ СВЕРКА С ЭТАЛОНОМ, и разница объявлена в заголовке пакета:
// второй независимой реализации Argon2 в этом окружении нет, а вписать вектор RFC по памяти было бы
// хуже отсутствия сверки. Значит, согласованно неверная реализация здесь пройдёт — и это записано,
// а не спрятано.

func TestNewHashesAreArgon2id(t *testing.T) {
	h, err := Hash("a-representative-passphrase")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(h, "argon2id$") {
		t.Fatalf("новый хеш не argon2id: %q", h)
	}
	if !Verify(h, "a-representative-passphrase") {
		t.Fatal("свежий хеш не проверяется собственной же функцией")
	}
	if Verify(h, "a-representative-passphrasE") {
		t.Fatal("проверка приняла ДРУГОЙ пароль")
	}
}

// ⚠ ГЛАВНОЕ СВОЙСТВО ЭТОГО PR: смена схемы не стоит НИ ОДНОГО сброшенного пароля. Хеш PBKDF2
// собирается здесь тем же кодом, что и раньше, и обязан продолжать проверяться.
func TestOldPBKDF2HashesKeepVerifying(t *testing.T) {
	const pw = "a-password-set-before-the-change"
	salt := make([]byte, saltBytes)
	for i := range salt {
		salt[i] = byte(i)
	}
	key := pbkdf2([]byte(pw), salt, DefaultIterations, keyBytes)
	enc := base64.RawStdEncoding
	old := fmt.Sprintf("%s$%d$%s$%s", scheme, DefaultIterations, enc.EncodeToString(salt), enc.EncodeToString(key))

	if !Verify(old, pw) {
		t.Fatal("СТАРЫЙ хеш перестал проверяться — смена схемы выкинула бы всех наружу")
	}
	if Verify(old, "wrong") {
		t.Fatal("старый хеш принял неверный пароль")
	}
	// И он объявлен устаревшим, чтобы вход его переписал.
	if !NeedsRehash(old) {
		t.Fatal("PBKDF2-хеш не помечен устаревшим — он никогда не будет обновлён")
	}
	// А свежий argon2id с текущими параметрами — нет.
	fresh, err := Hash(pw)
	if err != nil {
		t.Fatal(err)
	}
	if NeedsRehash(fresh) {
		t.Fatal("свежий хеш объявлен устаревшим — вход переписывал бы его на каждом входе")
	}
}

// Чувствительность к КАЖДОМУ параметру по отдельности. Одна проверка «поменяли что-то — ключ другой»
// прошла бы над реализацией, которая читает только один из трёх.
func TestEveryArgonParameterChangesTheKey(t *testing.T) {
	const pw = "a-representative-passphrase"
	salt := make([]byte, saltBytes)
	base := argon2.IDKey([]byte(pw), salt, argonTime, argonMemory, argonThreads, keyBytes)
	cases := []struct {
		name    string
		tm, mem uint32
		par     uint8
	}{
		{"время", argonTime + 1, argonMemory, argonThreads},
		{"память", argonTime, argonMemory * 2, argonThreads},
		{"параллелизм", argonTime, argonMemory, argonThreads + 1},
	}
	for _, c := range cases {
		got := argon2.IDKey([]byte(pw), salt, c.tm, c.mem, c.par, keyBytes)
		if subtle.ConstantTimeCompare(base, got) == 1 {
			t.Errorf("изменение параметра %q не изменило ключ — параметр не читается", c.name)
		}
	}
}

// Параметры едут С ХЕШЕМ и читаются обратно теми же значениями: без этого «поднять стоимость, не
// сбросив пароли» перестаёт работать в тот день, когда её поднимут.
func TestArgonParametersTravelAndParseBack(t *testing.T) {
	h, err := Hash("x-representative-passphrase")
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(h, "$")
	if len(parts) != 4 {
		t.Fatalf("хеш не из четырёх частей: %q", h)
	}
	mem, tm, par, ok := parseArgonParams(parts[1])
	if !ok {
		t.Fatalf("собственные параметры не разбираются: %q", parts[1])
	}
	if mem != argonMemory || tm != argonTime || par != argonThreads {
		t.Fatalf("прочитано m=%d t=%d p=%d, записано m=%d t=%d p=%d", mem, tm, par, argonMemory, argonTime, argonThreads)
	}
	// Хеш, сделанный под БОЛЕЕ СЛАБЫМИ параметрами, обязан проверяться своими и считаться устаревшим.
	weak := fmt.Sprintf("%s$m=%d,t=%d,p=%d$%s$%s", schemeArgon2id, argonMemory/2, argonTime, argonThreads,
		base64.RawStdEncoding.EncodeToString(make([]byte, saltBytes)),
		base64.RawStdEncoding.EncodeToString(argon2.IDKey([]byte("weak-pass-phrase"), make([]byte, saltBytes),
			argonTime, argonMemory/2, argonThreads, keyBytes)))
	if !Verify(weak, "weak-pass-phrase") {
		t.Fatal("хеш под прежними параметрами не проверяется своими же")
	}
	if !NeedsRehash(weak) {
		t.Fatal("более слабый хеш не помечен устаревшим")
	}
}

func TestVerifyRejectsMalformedArgon(t *testing.T) {
	good, _ := Hash("a-representative-passphrase")
	parts := strings.Split(good, "$")
	for _, bad := range []string{
		"argon2id$$" + parts[2] + "$" + parts[3],                    // пустые параметры
		"argon2id$m=0,t=3,p=1$" + parts[2] + "$" + parts[3],         // нулевая память
		"argon2id$m=65536,t=3$" + parts[2] + "$" + parts[3],         // не хватает p
		"argon2id$m=65536,t=3,p=1,x=9$" + parts[2] + "$" + parts[3], // лишний параметр
		"argon2id$m=x,t=3,p=1$" + parts[2] + "$" + parts[3],         // не число
		"unknown$m=65536,t=3,p=1$" + parts[2] + "$" + parts[3],      // чужая схема
	} {
		if Verify(bad, "a-representative-passphrase") {
			t.Errorf("испорченный хеш принят: %q", bad)
		}
		if !NeedsRehash(bad) {
			t.Errorf("испорченный хеш не помечен устаревшим: %q", bad)
		}
	}
}

// Двойник для выравнивания времени обязан быть ТОЙ ЖЕ схемы, иначе несуществующее имя отличается от
// существующего по задержке — канал, который вход закрывает намеренно.
func TestDummyHashMatchesTheCurrentScheme(t *testing.T) {
	d := DummyHash()
	if !strings.HasPrefix(d, "argon2id$") {
		t.Fatalf("двойник другой схемы: %q", d)
	}
	mem, tm, par, ok := parseArgonParams(strings.Split(d, "$")[1])
	if !ok || mem != argonMemory || tm != argonTime || par != argonThreads {
		t.Fatalf("двойник под другими параметрами: %q", d)
	}
	if Verify(d, "") || Verify(d, "anything") {
		t.Fatal("двойник совпал с паролем — он обязан не совпадать ни с чем")
	}
}
