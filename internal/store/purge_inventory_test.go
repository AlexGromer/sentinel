package store

// The gate that keeps docs/db-foreign-text.json true (ADR-100).
//
// An inventory of where foreign text lives is worth exactly as much as its freshness: the moment
// somebody adds a column and nobody classifies it, the document becomes a confident description of
// a database that no longer exists, and a cleanup built on it silently misses the new column.
//
// It is deliberately NOT a check on the text of internal/store/server.go. An assertion about the
// SHAPE OF SOURCE CODE is a surrogate for the thing it claims to check — mutations walk straight
// through those. This opens a real store with store.New, which runs the real DDL, and interrogates
// the database that actually results.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

type inventoryFile struct {
	Tables map[string]struct {
		Columns map[string]struct {
			Foreign string `json:"foreign"`
			Note    string `json:"note"`
		} `json:"columns"`
	} `json:"tables"`
}

func loadInventory(t *testing.T) inventoryFile {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "docs", "db-foreign-text.json"))
	if err != nil {
		t.Fatalf("read inventory: %v", err)
	}
	var inv inventoryFile
	if err := json.Unmarshal(b, &inv); err != nil {
		t.Fatalf("parse inventory: %v", err)
	}
	if len(inv.Tables) == 0 {
		t.Fatal("inventory parsed to zero tables — every assertion below would be vacuous")
	}
	return inv
}

// TestEveryStoredColumnIsClassified.
// Kills: a new column added to `schema` or `storeSchema` without an entry in the inventory.
// Kills: an inventory entry for a table or column that no longer exists (a stale document reads as
//
//	authoritative and is worse than an absent one).
func TestEveryStoredColumnIsClassified(t *testing.T) {
	inv := loadInventory(t)
	s := newDomServer(t) // runs the REAL New() -> the REAL DDL

	rows, err := s.db.Query(
		`SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name`)
	if err != nil {
		t.Fatal(err)
	}
	var tables []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			t.Fatal(err)
		}
		tables = append(tables, n)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	rows.Close()
	if len(tables) == 0 {
		t.Fatal("the store created no tables — this gate would pass vacuously")
	}

	valid := map[string]bool{"inherent": true, "incidental": true, "none": true}
	for _, tbl := range tables {
		entry, ok := inv.Tables[tbl]
		if !ok {
			t.Errorf("table %q exists in the store but is not classified in docs/db-foreign-text.json", tbl)
			continue
		}
		cols := columnsOf(t, s, tbl)
		for _, c := range cols {
			col, ok := entry.Columns[c]
			if !ok {
				t.Errorf("%s.%s exists in the store but is not classified in docs/db-foreign-text.json", tbl, c)
				continue
			}
			if !valid[col.Foreign] {
				t.Errorf("%s.%s: foreign=%q, want one of inherent|incidental|none", tbl, c, col.Foreign)
			}
			// A column that can hold foreign text must say something about what lands there. An
			// unexplained "inherent" is a label, and a label is not an inventory.
			if col.Foreign != "none" && col.Note == "" {
				t.Errorf("%s.%s is classified %q with no note — say what actually lands there",
					tbl, c, col.Foreign)
			}
		}
		// The reverse direction: the document must not describe columns that are gone.
		have := map[string]bool{}
		for _, c := range cols {
			have[c] = true
		}
		for c := range entry.Columns {
			if !have[c] {
				t.Errorf("docs/db-foreign-text.json classifies %s.%s, which no longer exists in the store", tbl, c)
			}
		}
	}
	for tbl := range inv.Tables {
		if !contains(tables, tbl) {
			t.Errorf("docs/db-foreign-text.json describes table %q, which the store does not create", tbl)
		}
	}
}

// TestEveryPurgeableTableIsInTheInventory ties the code to the document: purge.go's table registry
// and the inventory must name the same tables, or the tool would offer to clean something nobody
// classified — or refuse to clean something the inventory flags as holding foreign text.
//
// Kills: adding a table to `purgeable` without classifying it.
// Kills: a table the inventory marks as carrying foreign text silently dropping out of `purgeable`.
func TestEveryPurgeableTableIsInTheInventory(t *testing.T) {
	inv := loadInventory(t)
	for name, pt := range purgeable {
		entry, ok := inv.Tables[name]
		if !ok {
			t.Errorf("purgeable table %q is not in docs/db-foreign-text.json", name)
			continue
		}
		if _, ok := entry.Columns[pt.timeCol]; !ok {
			t.Errorf("purgeable[%q].timeCol = %q, which the inventory does not list as a column of that table",
				name, pt.timeCol)
		}
		if pt.capability == "" {
			t.Errorf("purgeable[%q] does not say what capability its purge takes away", name)
		}
	}
	// Anything the inventory says holds foreign text must be reachable by the cleanup, unless it is
	// explicitly documented as not purgeable (config).
	notPurgeable := map[string]bool{"config": true}
	for tbl, entry := range inv.Tables {
		if notPurgeable[tbl] || purgeable[tbl].timeCol != "" {
			continue
		}
		for col, c := range entry.Columns {
			if c.Foreign != "none" {
				t.Errorf("%s.%s is classified %q but %s is not purgeable and not documented as exempt",
					tbl, col, c.Foreign, tbl)
			}
		}
	}
}

func columnsOf(t *testing.T, s *Server, table string) []string {
	t.Helper()
	rows, err := s.db.Query(`SELECT name FROM pragma_table_info(?)`, table)
	if err != nil {
		t.Fatalf("table_info(%s): %v", table, err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			t.Fatal(err)
		}
		out = append(out, n)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(out) == 0 {
		t.Fatalf("table %s reported zero columns — the per-column assertions would be vacuous", table)
	}
	sort.Strings(out)
	return out
}

func contains(hay []string, needle string) bool {
	for _, h := range hay {
		if h == needle {
			return true
		}
	}
	return false
}
