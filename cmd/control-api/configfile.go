// The config domain's FILE tier (ADR-075). ADR-062 gave `/v1/config` a service tier only: with no
// store-gateway both GET and PUT answered 501 whose text read "this deployment keeps its config in a
// file (standalone tier)". No such file existed anywhere in the process — the 501 returned before the
// body was even read, and the readiness detail pointed at brain/runconfig.py, a YAML the OPERATOR
// passes to agentctl and the server never writes. So the wizard's 💾 was a soft refusal describing a
// tier that had never been built, and a standalone operator's whole configuration went nowhere.
//
// This file is that tier. The document, the validation and the key are the SAME in both tiers — only
// the medium differs — so a deployment that later gains a store-gateway is not configuring a different
// product.
//
// WHY NOT ALWAYS FALL BACK TO THE FILE: `s.store` is nil in two very different situations. If
// CONTROL_API_STORE_ADDR is unset, the operator chose the standalone tier and the file IS the config.
// If it is SET but the gateway did not answer at boot (main.go fail-open: it only warns, and never
// re-dials), silently writing a file would be the same silent degradation this milestone exists to
// close — the gateway comes back, the store wins on the next read, and the operator's saved settings
// vanish with no message. So that case refuses honestly instead (503 + the reason), and only true
// standalone writes a file.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// configFileName is the standalone tier's document, under <repo>/state (already a mounted volume in
// docker-compose.yml, so it survives a container restart exactly as the SQLite databases beside it do).
const configFileName = "config.json"

// configTier names which medium answered a config request. It travels on the API response because
// "saved" alone cannot distinguish a store write from a file write, and an operator who believes they
// are writing to a shared store has to be told when they are not.
type configTier string

const (
	tierStore configTier = "store"
	tierFile  configTier = "file"
	// tierUnavailable = a store-gateway was configured and is not answering. Not a tier the request can
	// fall back to; see the WHY NOT ALWAYS FALL BACK note above.
	tierUnavailable configTier = "unavailable"
)

// configTier reports which tier serves config right now.
func (s *server) configTier() configTier {
	switch {
	case s.store != nil:
		return tierStore
	case s.storeAddr != "":
		return tierUnavailable
	default:
		return tierFile
	}
}

func (s *server) configFilePath() string {
	return filepath.Join(s.repo, "state", configFileName)
}

// configFileDoc is the on-disk envelope. It carries the same three things as the store's ConfigRecord —
// key, document, updated_at — so GET and the readiness probe need not know which tier answered.
//
// One deliberate difference: the store keeps `value_json` as an ESCAPED STRING (it is a database
// column), while the file embeds the document as real JSON. A config file exists to be opened, read and
// occasionally hand-edited by whoever runs the deployment; a wall of \"-escaped text would make that
// hostile for no gain. The round trip is unaffected — the HTTP layer parses either shape into the same
// document.
type configFileDoc struct {
	Key       string          `json:"key"`
	ValueJson json.RawMessage `json:"value_json"`
	UpdatedAt string          `json:"updated_at"`
}

// readConfigFile returns the stored document, or ok=false when there is none (a missing file is the
// legitimate "nothing saved yet" state, not an error). A CORRUPT file is reported as an error: telling
// the operator "no config" when a config exists but cannot be parsed would send them to re-run the
// wizard over a file they might rather fix — the same distinction the service tier draws between a
// gateway hiccup and a genuine miss.
func (s *server) readConfigFile() (doc *configFileDoc, ok bool, err error) {
	b, rerr := os.ReadFile(s.configFilePath())
	if rerr != nil {
		if os.IsNotExist(rerr) {
			return nil, false, nil
		}
		return nil, false, rerr
	}
	var d configFileDoc
	if uerr := json.Unmarshal(b, &d); uerr != nil {
		return nil, false, fmt.Errorf("stored config file is not valid JSON: %w", uerr)
	}
	if len(d.ValueJson) == 0 {
		return nil, false, fmt.Errorf("stored config file carries no document")
	}
	return &d, true, nil
}

// writeConfigFile persists body (already validated by configguard + validateLoggingSection, exactly as
// the store tier is) atomically: a temp file in the same directory, then rename. A half-written config
// is worse than none — the reader above would call it corrupt and the run path would fall back to no
// persisted LLM at all. 0600 because the document names internal hosts and models; configguard already
// guarantees it carries no secret, but the deployment shape is not public either.
func (s *server) writeConfigFile(body string) error {
	path := s.configFilePath()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	doc := configFileDoc{
		Key:       setupConfigKey,
		ValueJson: json.RawMessage(body),
		UpdatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	enc, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".config-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename; cleans up every failure path
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(enc); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil { // rename is atomic, but only over data that reached the disk
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// storeUnavailableMsg is the one sentence both GET and PUT use for the configured-but-down case, so the
// wizard never has to guess which of the two nil-store situations it is looking at.
const storeUnavailableMsg = "a store-gateway is configured (CONTROL_API_STORE_ADDR) but did not answer at " +
	"startup, so config cannot be read or written; start it and restart the control-API — this deployment " +
	"deliberately does not fall back to a file, because the gateway would win again on the next read"

// storeUnavailableMsgRU is the same sentence for a Russian reader. It lives HERE, one line below its
// English half, for the reason componentNote gives: a translation kept anywhere else drifts the first
// time somebody edits one of the two, and nothing compares them. [HEALTH-REASON-EN] / W6.
const storeUnavailableMsgRU = "хранилище store-gateway объявлено (CONTROL_API_STORE_ADDR), но не ответило " +
	"при старте, поэтому конфигурацию нельзя ни прочитать, ни записать; запустите его и перезапустите " +
	"control-API — это развёртывание намеренно не откатывается на файл, потому что при следующем чтении " +
	"снова победит шлюз"
