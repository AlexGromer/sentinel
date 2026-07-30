// Package identity holds the password KDF for ADR-109 local accounts.
//
// PBKDF2-HMAC-SHA256 (RFC 8018 §5.2), stdlib only. The alternative was x/crypto/bcrypt, and it was
// rejected on a concrete obstacle rather than a preference: adding that module resolves a transitive
// golang.org/x/net the build cache does not carry, so the dependency cannot be added without network
// access, and the air-gapped build (docker-compose.offline.yml, scripts/offline-verify.sh) is a
// promise this project already makes. PBKDF2 is a composition of two stdlib primitives whose whole
// definition is a loop, which is why it is safe to assemble here and bcrypt would not have been.
//
// It is not trusted on inspection. TestPBKDF2MatchesReference checks this implementation against
// Python's hashlib.pbkdf2_hmac — a separate, independently-written implementation that ships with the
// interpreter the brain already runs on. Vectors are DERIVED by running it, never typed in from
// memory: a constant recalled rather than computed would agree with a wrong implementation exactly
// when it mattered.
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
)

// Iterations for a NEW hash. Stored per-hash (see Hash's encoding) so raising this number does not
// invalidate existing passwords — an old hash keeps verifying with the count it was made under, and
// gets the new one whenever it is next set.
const DefaultIterations = 600_000

const (
	saltBytes = 16
	keyBytes  = 32
	scheme    = "pbkdf2-sha256"
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

// Hash derives a storable credential: "pbkdf2-sha256$<iterations>$<salt-b64>$<key-b64>".
//
// The scheme name and the iteration count travel WITH the hash rather than living in a constant the
// verifier reads. A verifier that assumes today's parameters cannot check a hash made under
// yesterday's, which is how a policy change locks every existing user out.
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
	key := pbkdf2([]byte(password), salt, DefaultIterations, keyBytes)
	enc := base64.RawStdEncoding
	return fmt.Sprintf("%s$%d$%s$%s", scheme, DefaultIterations, enc.EncodeToString(salt), enc.EncodeToString(key)), nil
}

// Verify reports whether password produced stored. False for anything malformed — a hash this code
// cannot parse is not a password that matches.
func Verify(stored, password string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 4 || parts[0] != scheme {
		return false
	}
	iter, err := strconv.Atoi(parts[1])
	if err != nil || iter < 1 {
		return false
	}
	enc := base64.RawStdEncoding
	salt, err1 := enc.DecodeString(parts[2])
	want, err2 := enc.DecodeString(parts[3])
	if err1 != nil || err2 != nil || len(want) == 0 {
		return false
	}
	got := pbkdf2([]byte(password), salt, iter, len(want))
	// Constant-time, matching how the machine bearer is compared (cmd/control-api/main.go): a timing
	// difference on a credential check is a credential leak.
	return subtle.ConstantTimeCompare(got, want) == 1
}

// NeedsRehash reports whether stored was made under weaker parameters than the current policy, so a
// successful login can quietly upgrade it.
func NeedsRehash(stored string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 4 || parts[0] != scheme {
		return true
	}
	iter, err := strconv.Atoi(parts[1])
	return err != nil || iter < DefaultIterations
}
