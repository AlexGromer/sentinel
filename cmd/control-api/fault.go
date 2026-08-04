package main

// HEALTH-004 — "WE broke" and "YOUR APPLICATION broke" must read differently.
//
// The product could already refuse to start when a required component was missing (HEALTH-001) and
// could already say every error out loud (HEALTH-002). What it could not do was tell the two apart
// on the surface a person actually looks at. The measured shape of that gap:
//
//   - `verdictEnum` mapped exit 0/2/3 to three words and EVERYTHING ELSE to `problem`. So exit 1
//     ("the test found a problem in your application"), exit 4 ("our own code threw", ADR-087) and
//     exit -1 ("we were killed by a signal") arrived at the dashboard as one word, and that word
//     sends the reader to go and debug their application.
//   - exit 3 was worse than coarse, it was WRONG. HEALTH-001 gave refusals exit 3, which already
//     meant `integrity`, so a run refused because the model endpoint was down rendered as
//     "ЦЕЛОСТНОСТЬ / КОНФИГУРАЦИЯ · несовпадение plan_hash/golden — нужен человек". No plan_hash was
//     involved. That screenshot (06-goal-without-a-model.png, the ui-smoke artifact) is the reason
//     this file exists.
//
// WHY A SEPARATE AXIS AND NOT MORE VERDICT WORDS (Alex's decision, 2026-08-03): `problem` is the
// OUTCOME and stays one. Whose problem it is, is a different question, and answering it by adding
// words multiplies combinatorially — `problem` x 4 domains, then `regression` x 4. Worse, two
// mechanisms describing one fact drift apart; this codebase already has that exact wound in
// `lvKindOf` disagreeing with the event catalogue about audiences.
//
// WHERE THE ANSWER COMES FROM. Not from a table in this file: the catalogue already knows, because
// the code that ENDED the run knows. `fatal.llm_required_unreachable` is `tool` by declaration,
// `import.files_skipped` is `test`, `fatal.target_unset` is `config`. The exit code is only the
// fallback for a run whose log we could not read — and the fallback lives in the catalogue too
// (`exit_codes[N].fault`), so this file holds the ORDER of the questions and nothing else.

import (
	eventcatalog "github.com/AlexGromer/sentinel/brain"
)

// faultDomain answers "whose problem is this run's outcome" for a FINISHED run.
//
// state is the run record's terminal state (done / failed / canceled), exit its exit code, and
// terminalCode the last catalogued code that declared a fault (logSink.terminalFault).
//
// Order matters and is the whole design:
//
//  1. A run that never became a process is ours — there is no application in the story yet.
//  2. A deliberate stop is nobody's fault. It is not a failure and must not be filed as one.
//  3. A GREEN ending has no fault, whatever happened along the way.
//  4. The code that ended the run wins, because it is the only witness with the precise answer.
//  5. Only then the exit code, from the catalogue's own table.
//
// Returns "" only when the exit code is one the catalogue never declared. That is deliberate: an
// unexplainable exit is itself worth surfacing, and inventing `tool` for it would quietly re-create
// the guessing this axis removes.
func faultDomain(state string, exit int, terminalCode string) string {
	switch state {
	case "failed":
		// Could not spawn agentctl at all. The application under test was never contacted, so
		// attributing this anywhere but to us would be a lie with a plausible shape.
		return "tool"
	case "canceled":
		return "none"
	}
	// A run that exited 0 harms nobody, and this rule has to come BEFORE the terminal code. Several
	// codes that name a fault describe a degradation a run then SURVIVES — falling back to the
	// heuristic planner is the common one — and without this line the last such code would stick a
	// blame chip onto a green run. What was lost on the way is a different question, already answered
	// separately by `degrades` and by the refined pass_with_* verdicts; this axis answers "who broke
	// this ending", and a green ending is not broken.
	if exit == 0 {
		return "none"
	}
	if f := eventcatalog.FaultOf(terminalCode); f != "" {
		return f
	}
	if e, ok := eventcatalog.ExitInfoOf(exit); ok {
		return e.Fault
	}
	return ""
}
