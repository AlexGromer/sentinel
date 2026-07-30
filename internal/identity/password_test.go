package identity

import (
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
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
