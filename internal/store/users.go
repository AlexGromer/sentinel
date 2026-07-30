package store

// ADR-109 local accounts. The gateway STORES a credential and never judges one: verification lives in
// control-api, which is already the authentication boundary and already holds the KDF
// (internal/identity). Putting the KDF here as well would give one rule two implementations, which is
// the drift M16 exists to remove.
//
// Deliberately NOT a general user directory. There is no email, no profile, no group, no role beyond
// `is_admin` — OIDC/SSO/RBAC and everything that comes with them stay commercial (ADR-056). What is
// here is the minimum that lets two people on one deployment stop reading each other's runs.

import (
	"context"
	"database/sql"
	"fmt"
	"strings"

	"github.com/AlexGromer/sentinel/internal/store/pb"
)

// userWhere resolves a UserRef to a WHERE clause. Either field identifies: login arrives with a name,
// everything else with an id. An empty ref matches NOTHING rather than the first row — a lookup with
// no subject must not silently return an account.
func userWhere(ref *pb.UserRef) (string, []any, error) {
	switch {
	case ref == nil:
		return "", nil, fmt.Errorf("store: user reference is nil")
	case ref.UserId != "":
		return "user_id=?", []any{ref.UserId}, nil
	case strings.TrimSpace(ref.Name) != "":
		return "name=?", []any{strings.TrimSpace(ref.Name)}, nil
	}
	return "", nil, fmt.Errorf("store: user reference carries neither user_id nor name")
}

func (s *Server) UpsertUser(_ context.Context, u *pb.User) (*pb.Empty, error) {
	if u == nil || u.UserId == "" || strings.TrimSpace(u.Name) == "" {
		return nil, fmt.Errorf("store: a user needs both a user_id and a name")
	}
	if u.PwHash == "" {
		// Fail closed. A row with no credential would be an account that Verify() can never satisfy but
		// that still occupies its name — indistinguishable, from the outside, from a locked-out person.
		return nil, fmt.Errorf("store: refusing to store user %q with no credential", u.Name)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec(
		`INSERT INTO users(user_id,name,pw_hash,is_admin,created_at)
		 VALUES(?,?,?,?,?)
		 ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,pw_hash=excluded.pw_hash,
		   is_admin=excluded.is_admin`,
		u.UserId, strings.TrimSpace(u.Name), u.PwHash, boolToInt(u.IsAdmin), nowRFC3339(u.CreatedAt))
	return &pb.Empty{}, err
}

func (s *Server) GetUser(_ context.Context, ref *pb.UserRef) (*pb.User, error) {
	where, args, err := userWhere(ref)
	if err != nil {
		return nil, err
	}
	u := &pb.User{}
	var admin int
	err = s.db.QueryRow(
		"SELECT user_id,name,pw_hash,is_admin,created_at FROM users WHERE "+where, args...).Scan(
		&u.UserId, &u.Name, &u.PwHash, &admin, &u.CreatedAt)
	if err == sql.ErrNoRows {
		// Found=false rather than an error: "no such account" is a normal answer to a login attempt, and
		// an error here would make a wrong username look like a broken store.
		return &pb.User{Found: false}, nil
	}
	if err != nil {
		return nil, err
	}
	u.IsAdmin = admin != 0
	u.Found = true
	return u, nil
}

func (s *Server) ListUsers(_ context.Context, _ *pb.Empty) (*pb.UserList, error) {
	out := &pb.UserList{}
	if err := s.db.QueryRow("SELECT COUNT(*) FROM users").Scan(&out.Total); err != nil {
		return nil, err
	}
	rows, err := s.db.Query("SELECT user_id,name,is_admin,created_at FROM users ORDER BY created_at")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		u := &pb.User{Found: true}
		var admin int
		if err := rows.Scan(&u.UserId, &u.Name, &admin, &u.CreatedAt); err != nil {
			return nil, err
		}
		u.IsAdmin = admin != 0
		// pw_hash is NOT selected. A list is for showing people who exists, and a credential that never
		// enters the reply cannot be logged, cached or rendered by a caller that meant no harm.
		out.Users = append(out.Users, u)
	}
	return out, rows.Err()
}

func (s *Server) DeleteUser(_ context.Context, ref *pb.UserRef) (*pb.Empty, error) {
	where, args, err := userWhere(ref)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	// Rows the account owned are LEFT ALONE, and that is a decision rather than an omission. Deleting a
	// person's runs with them would destroy history someone else may be relying on, and cascading from
	// an account removal is the kind of deletion nobody expects to have authorised. They become unowned
	// — visible to a machine token, and adoptable — which is recoverable; a cascade is not.
	_, err = s.db.Exec("DELETE FROM users WHERE "+where, args...)
	return &pb.Empty{}, err
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
