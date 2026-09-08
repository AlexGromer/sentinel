package main

// Alex's directive, extending ADR-109:
//
//	"Everything the working person owns — artefacts, settings, objects — belongs to them. Only the
//	 global settings and the tool belong to the master user (the admin), who configures the tool for
//	 everyone."
//
// The config domain was the last place that ignored it: one document under a bare key, writable by
// anyone holding any credential. Splitting it needs an answer to "which settings are the TOOL's?",
// and the answer is not a taste — the document's own sections already divide along it:
//
//	llm, settings, logging  — the tool. Which model backend this deployment talks to, how long its
//	                          artefacts live on ITS disk, which gates fail a run, what it logs. Every
//	                          one is an environment variable the server process reads; changing one
//	                          changes what the installation does for everybody.
//	run, auth               — the person. The budget I run with, my storage_state path, my planner,
//	                          my coverage target. Nobody else's run is affected by mine.
//
// A section with no declared scope is REFUSED, not defaulted. Defaulting to global would let a new
// section quietly become admin-only; defaulting to personal would let it quietly become per-account.
// Both are decisions, and a decision nobody made is the one that gets made wrong.
//
// WHO gets a personal document: whoever the caller is. A machine caller and a deployment with no
// accounts both have owner "" and therefore write the GLOBAL document — the same one rule as
// everywhere else in ADR-109 ("no subject, no scoping"), which is also what keeps the setup wizard
// working unchanged: it authenticates with the machine token, so the run/auth defaults it saves are
// the tool's defaults, exactly as they were before this split existed.
//
// Note on the spawn path: `llm`, `settings` and `logging` are what control-api materialises into a
// run's environment, and all three are global — so mergedPersistedEnv keeps reading the global
// document and needs no notion of ownership. The personal sections are form defaults the interface
// fills in, not server-side environment; a personal `run.max_steps` reaches a run as a field on
// POST /v1/runs, where it already travelled.

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

type configScope string

const (
	scopeGlobal configScope = "global"
	scopeUser   configScope = "user"
)

// configSectionScope is the single source of truth for the split. GET /v1/config-schema publishes it
// verbatim, so the UI disables what a caller may not change instead of guessing — and instead of
// letting them fill in a form whose save is going to be refused.
var configSectionScope = map[string]configScope{
	"llm":      scopeGlobal,
	"settings": scopeGlobal,
	"logging":  scopeGlobal,
	"run":      scopeUser,
	"auth":     scopeUser,
}

// configSections returns the declared section names, sorted — for error messages and the schema, both
// of which are read by people and must not reorder between calls.
func configSections() []string {
	out := make([]string, 0, len(configSectionScope))
	for k := range configSectionScope {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// splitConfigDoc divides an incoming document by declared scope.
//
// It returns the two halves as raw sections rather than re-marshalling the whole document, so the
// bytes a caller sent are the bytes that get stored: a round trip through map[string]any would
// reorder members and rewrite numbers (1 becoming 1e+00), turning "save my config" into "save
// something equivalent to my config".
func splitConfigDoc(body []byte) (global, personal map[string]json.RawMessage, err error) {
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, nil, fmt.Errorf("config must be a JSON object: %w", err)
	}
	global, personal = map[string]json.RawMessage{}, map[string]json.RawMessage{}
	var unknown []string
	for name, raw := range doc {
		switch configSectionScope[name] {
		case scopeGlobal:
			global[name] = raw
		case scopeUser:
			personal[name] = raw
		default:
			unknown = append(unknown, name)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return nil, nil, fmt.Errorf("unknown configuration section(s) %s: every section must declare whether it "+
			"belongs to the tool (admin) or to the person using it, and this one declares neither — known sections are %s",
			strings.Join(unknown, ", "), strings.Join(configSections(), ", "))
	}
	return global, personal, nil
}

// mergeConfigDocs overlays a personal document on the global one and reports where each section came
// from. The overlay is per SECTION, not per field: a person who set `run` owns the whole answer to
// "how do my runs start", and half-merging two `run` blocks would produce a configuration neither
// party wrote — with no way for either to see what the other contributed.
func mergeConfigDocs(global, personal map[string]json.RawMessage) (map[string]json.RawMessage, map[string]string) {
	out := map[string]json.RawMessage{}
	sources := map[string]string{}
	for k, v := range global {
		out[k], sources[k] = v, string(scopeGlobal)
	}
	for k, v := range personal {
		out[k], sources[k] = v, string(scopeUser)
	}
	return out, sources
}

// mayWriteGlobal reports whether this caller may change the tool's own configuration.
func mayWriteGlobal(c caller) bool { return c.machine || c.admin }

// globalSectionsIn names the global sections present in a document, for the refusal message. A 403
// that does not say WHICH member was refused leaves the caller to bisect their own document.
func globalSectionsIn(global map[string]json.RawMessage) string {
	names := make([]string, 0, len(global))
	for k := range global {
		names = append(names, k)
	}
	sort.Strings(names)
	return strings.Join(names, ", ")
}

// marshalConfigDoc re-serialises a section map with sorted keys, so the same document always produces
// the same bytes — a stored config that differs only in member order would show as a change in every
// diff an operator takes of it.
func marshalConfigDoc(doc map[string]json.RawMessage) ([]byte, error) {
	keys := make([]string, 0, len(doc))
	for k := range doc {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		key, err := json.Marshal(k)
		if err != nil {
			return nil, err
		}
		b.Write(key)
		b.WriteByte(':')
		b.Write(doc[k])
	}
	b.WriteByte('}')
	return []byte(b.String()), nil
}

// ── ЛИЧНЫЕ УМОЛЧАНИЯ ПРОГОНА ─────────────────────────────────────────────────────────────────────
//
// ⚠ ЗАМЕР, РАДИ КОТОРОГО ЭТО НАПИСАНО. Секции `run` и `auth` объявлены настраиваемыми, мастер их
// сохраняет, `GET /v1/config` их отдаёт — и НИКТО их не читал. В окружение прогона материализуются
// только глобальные секции, а личные, по замыслу, были «умолчаниями формы, которые заполняет
// интерфейс». Интерфейс их не заполнял: обращений к `cfgDoc.run` в хабе не было ни одного. То есть
// человек сохранял target/goal/mode/planner/бюджеты, получал подтверждение сохранения — и не
// действовало НИЧЕГО. Худшая форма отказа: интерфейс подтверждает, и человек считает, что настроил.
//
// ПОЧЕМУ ЧИТАТЕЛЬ ЗДЕСЬ, А НЕ В ХАБЕ. Читатель на стороне интерфейса чинил бы одну дверь из трёх:
// тело `POST /v1/runs` шлют ещё чат, заглушка `/v1/chat/completions` и мастер настройки, и каждому
// пришлось бы повторить заполнение. Сервер — единственное место, общее для всех четырёх.
//
// ⚠ И ГЛАВНОЕ: «НЕ ВЫБИРАЛ» НЕ ПРЕВРАЩАЕТСЯ В «ВЫБРАЛ». Умолчание применяется ТОЛЬКО к полю,
// которого запрос не нёс, и применённое НАЗЫВАЕТСЯ в ответе. Код рядом запрещает смешивать эти два
// факта прямым текстом («an invisible default makes "I did not choose" and "I chose exactly this"
// the same act, and then nobody can say what the run will produce»), и запрет соблюдён: невидимым
// умолчание не становится — оно возвращается вызывателю поимённо.

// personalRunDefaults — какие поля запроса заполняются из какой сохранённой секции. Перечень
// закрытый и парный: ключ секции слева, адрес поля справа. Секции те же, что объявлены
// пользовательскими в configSectionScope, — иначе появилась бы третья классификация тех же данных.
var personalRunDefaults = []struct {
	section string
	key     string
	get     func(*runRequest) string
	set     func(*runRequest, string)
}{
	{"run", "mode", func(r *runRequest) string { return r.Mode }, func(r *runRequest, v string) { r.Mode = v }},
	{"run", "planner", func(r *runRequest) string { return r.Planner }, func(r *runRequest, v string) { r.Planner = v }},
	{"run", "target", func(r *runRequest) string { return r.Target }, func(r *runRequest, v string) { r.Target = v }},
	{"run", "goal", func(r *runRequest) string { return r.Goal }, func(r *runRequest, v string) { r.Goal = v }},
	{"run", "describe", func(r *runRequest) string { return r.Describe }, func(r *runRequest, v string) { r.Describe = v }},
	{"run", "coverage_target", func(r *runRequest) string { return r.CoverageTarget }, func(r *runRequest, v string) { r.CoverageTarget = v }},
	{"run", "max_steps", func(r *runRequest) string { return r.MaxSteps }, func(r *runRequest, v string) { r.MaxSteps = v }},
	{"run", "plan_budget", func(r *runRequest) string { return r.PlanBudget }, func(r *runRequest, v string) { r.PlanBudget = v }},
	{"run", "heal_budget", func(r *runRequest) string { return r.HealBudget }, func(r *runRequest, v string) { r.HealBudget = v }},
	{"run", "total_budget", func(r *runRequest) string { return r.TotalBudget }, func(r *runRequest, v string) { r.TotalBudget = v }},
	{"auth", "storage_state", func(r *runRequest) string { return r.StorageState }, func(r *runRequest, v string) { r.StorageState = v }},
	{"auth", "storage_state_save", func(r *runRequest) string { return r.StorageStateSave }, func(r *runRequest, v string) { r.StorageStateSave = v }},
	{"auth", "login_plan", func(r *runRequest) string { return r.LoginPlan }, func(r *runRequest, v string) { r.LoginPlan = v }},
}

// scalarString приводит сохранённое значение к строке, в которой его ждёт запрос. Числа в JSON
// приходят float64, и `40` обязано стать "40", а не "40.000000" — иначе унаследованный потолок
// шагов доедет до agentctl мусором и прогон упадёт по причине, к настройке не относящейся.
func scalarString(v any) string {
	switch x := v.(type) {
	case string:
		return strings.TrimSpace(x)
	case bool:
		if x {
			return "1"
		}
		return "0"
	case float64:
		if x == float64(int64(x)) {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	case json.Number:
		return x.String()
	}
	return ""
}

// applyPersonalRunDefaults заполняет пустые поля запроса из ЛИЧНОГО документа вызывающего и
// возвращает имена заполненных — их называет ответ. Молча возвращает пусто, когда владельца нет
// (машинный кредентиал, развёртывание без аккаунтов) или хранилище недоступно: прогон, отказавшийся
// стартовать из-за необязательных умолчаний, был бы хуже прогона без них.
func (s *server) applyPersonalRunDefaults(req *runRequest) []string {
	if req.owner == "" || s.store == nil {
		return nil
	}
	rec, err := s.store.getConfig(setupConfigKey, req.owner, storeCallTimeout)
	if err != nil || rec == nil || rec.ValueJson == "" {
		return nil
	}
	var doc map[string]json.RawMessage
	if json.Unmarshal([]byte(rec.ValueJson), &doc) != nil {
		return nil
	}
	sections := map[string]map[string]any{}
	for name := range configSectionScope {
		if configSectionScope[name] != scopeUser {
			continue
		}
		raw, ok := doc[name]
		if !ok {
			continue
		}
		var m map[string]any
		if json.Unmarshal(raw, &m) == nil {
			sections[name] = m
		}
	}
	var used []string
	for _, f := range personalRunDefaults {
		if f.get(req) != "" {
			continue // человек выбрал сам — умолчание не спорит с выбором
		}
		v, ok := sections[f.section][f.key]
		if !ok {
			continue
		}
		if sv := scalarString(v); sv != "" {
			f.set(req, sv)
			used = append(used, f.section+"."+f.key)
		}
	}
	// pw_no_trace — указатель, поэтому «не выбирал» у него выражается nil, а не пустой строкой.
	if req.PWNoTrace == nil {
		if v, ok := sections["auth"]["pw_no_trace"]; ok {
			if b, isBool := v.(bool); isBool {
				req.PWNoTrace = &b
				used = append(used, "auth.pw_no_trace")
			}
		}
	}
	sort.Strings(used)
	return used
}
