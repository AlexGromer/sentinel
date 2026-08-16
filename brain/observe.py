"""Sentinel — how much of a run is OBSERVED. One decision, in one place (LIVE-MATRIX, ADR-120).

WHAT WAS WRONG, measured before this file existed: four unrelated switches, read in four unrelated
places across three processes and two languages, and NOTHING that decided them together.

    PW_HEADED / PW_HEADLESS      pw-executor/src/launch.ts:26      (executor process)
    SENTINEL_TRACE_SCREENSHOTS   pw-executor/src/server.ts:489     (executor process)
    SENTINEL_LIVE_FRAMES         brain/graph.py:45                 (this process)
    PW_NO_TRACE                  brain/__main__.py + server.ts:610 (NOT a mode — see below)

A person could not answer "what will I see, and what does it cost", because no single place knew.
Worse, ONE picture — the per-step frame — is gated by TWO of them in TWO languages, so switching one
off produced a half-observed run that said nothing about the missing half.

WHO CHOOSES (decision, Alex 2026-08-10). The PERSON does, and in the interface: a deployment default
in settings, overridden per run in the form, and the same name as a CLI flag. The inherited default
is SHOWN rather than implied — an invisible default makes "I did not choose" and "I chose exactly
this" the same act, and then nobody can say what a run will produce.

⚠ PW_NO_TRACE IS NOT IN THE SET, and this file must never write it. It is a fail-closed SECRET guard
with two independent enforcement points: `browser.fill` throws when a secretRef is entered while
tracing is active (pw-executor/src/server.ts:610), and a replay carrying a secretRef exits 3 before
starting (brain/__main__.py). A mode called `off` that "turns observation off" by clearing it would
silently remove that guard; one that set it would break every login run. Both directions are wrong,
so it stays out of the set and out of the interface.

⚠ MODES THIS BUILD CANNOT DO ARE REFUSED, NOT FAKED. A mode is declared here because the set is the
product's answer to "what can I ask for", but a declaration is not machinery: until the machinery
lands, asking for one is refused with the task named. Accepting a mode and quietly doing something
else is the class of silence this whole arc exists to remove — a person who asked to watch would get
frames and no reason. `NOT_YET` IS NOW EMPTY: `human` left it with LIVE-HUMAN and `record` with
LIVE-RECORD (ADR-125), each together with the switch that performs it — a mode leaves the waiting
room only alongside the thing that does the work. The mapping stays because the shape is the
contract: the next declared-but-unbuilt mode belongs in it, not in an `if`.

⚠ AN IMPOSSIBILITY IS NOT THE SAME AS AN UNBUILT MODE, and ADR-125 separates them. `record` is built,
but a run that attached to somebody else's browser over CDP cannot carry it: `recordVideo` is a
`newContext` option and that context was adopted, not created. Unlike `slowMo` — which the executor
compensates for with its own pauses (see launch.ts) — video has NOTHING to pay with, so a `record`
run over CDP would end having produced no video at all. That is refused at the door rather than
degraded, because a run that did not do the one thing it was asked for is not a degraded run.

⚠ THE DECORATION SWITCH IS ONE HALF OF A PAIR. `SENTINEL_DECORATE` is written HERE and nowhere else,
and read by pw-executor and nowhere else (LIVE-HUMAN). It is not a third frame switch: frames answer
"is anything captured", decoration answers "is the picture drawn for a PERSON". They are separate
because a decorated frame is the wrong input for a model and for a golden — hence the executor lifts
the overlay AROUND such a capture instead of switching decoration off for the run (ADR-120).
"""
import os

OFF = "off"        # nothing: CI and speed. The trace keeps its own rule (ADR-084: kept when exit != 0)
FRAMES = "frames"  # one screenshot per step — what the hub renders today
STREAM = "stream"  # frames + the live screencast, undecorated: usable by a person AND by a machine
HUMAN = "human"    # stream + cursor + slowMo + highlight — CHANGES TIMING (LIVE-HUMAN)
RECORD = "record"  # a video file as an artifact, orthogonal to live (LIVE-RECORD)

MODES = (OFF, FRAMES, STREAM, HUMAN, RECORD)
DEFAULT = FRAMES

#: Modes whose machinery is not in this build. Declared as a mapping rather than an `if` chain so the
#: gate can walk it, and so adding the machinery is a deletion here rather than a search.
#:
#: EMPTY as of ADR-125, and that is a state worth naming rather than a leftover: every mode this
#: product declares, it now performs. The mapping is kept — deleting it would make the next
#: declared-but-unbuilt mode arrive as an `if`, which is the shape this replaced.
NOT_YET: dict = {}

#: The environment switches this resolver OWNS: `apply` writes exactly these and `overrides` reports
#: exactly these, both by walking this tuple. Two hand-kept lists is how the four switches drifted
#: apart in the first place — a switch added to one and forgotten in the other would be set silently
#: and then contradict the plan in the log without saying so.
SWITCHES = ("SENTINEL_LIVE_FRAMES", "SENTINEL_TRACE_SCREENSHOTS", "SENTINEL_DECORATE", "SENTINEL_RECORD")

#: What each mode COSTS, in the words the interface must show beside it. A mode that only has a name
#: leaves a person choosing by vibe; ADR-120 requires the applicability and the price on screen.
COST = {
    OFF: {"ru": "ничего не снимается — быстрее всего; смотреть будет не на что",
          "en": "nothing is captured — fastest; there will be nothing to look at"},
    FRAMES: {"ru": "кадр на каждый шаг, их показывает хаб; замедляет прогон незначительно",
             "en": "one frame per step, rendered by the hub; slows a run slightly"},
    STREAM: {"ru": "живой экран без украшений — годится и человеку, и машине; стоит соединения, пока смотрят",
             "en": "the live screen, undecorated — usable by a person and a machine; costs a connection while watched"},
    HUMAN: {"ru": "курсор, замедление и подсветка. ⚠ МЕНЯЕТ ТАЙМИНГ: такой прогон не годится для проверки "
                  "откликов, гонок и таймаутов, и не смешивается с эталонным режимом",
            "en": "cursor, slowdown and highlight. ⚠ CHANGES TIMING: not usable for checking response times, "
                  "races or timeouts, and does not mix with golden mode"},
    RECORD: {"ru": "видеофайл артефактом после прогона, С КУРСОРОМ — запись без него так же нечитаема, "
                   "как голый скринкаст. ⚠ ЗНАЧИТ МЕНЯЕТ ТАЙМИНГ ровно как human, и эталоном такой "
                   "прогон быть не может. ⚠ НЕВОЗМОЖЕН при подключении к чужому браузеру по CDP",
             "en": "a video file as an artifact after the run, WITH THE CURSOR — a recording without one "
                   "is as unreadable as a bare screencast. ⚠ SO IT CHANGES TIMING exactly as human does, "
                   "and such a run cannot be a golden. ⚠ IMPOSSIBLE when attached to a foreign browser over CDP"},
}


class ObservationPlan:
    """What this run observes, and WHY — the reason is not decoration.

    A run that quietly observes less than the person expected is indistinguishable from a run whose
    observation broke. `why` rides into the run log so the difference is visible when it matters
    rather than reconstructed afterwards.
    """

    __slots__ = ("mode", "frames", "decorations", "video", "why")

    def __init__(self, mode, frames, decorations, video, why):
        self.mode = mode
        self.frames = frames
        self.decorations = decorations
        self.video = video
        self.why = why

    def as_dict(self):
        return {"mode": self.mode, "frames": self.frames, "decorations": self.decorations,
                "video": self.video, "why": self.why}

    def __eq__(self, other):
        return isinstance(other, ObservationPlan) and self.as_dict() == other.as_dict()

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"ObservationPlan({self.as_dict()})"


class Refusal(Exception):
    """An observation request the product will not pretend to satisfy.

    Raised BEFORE a run starts, so it costs nothing and cannot half-happen. The caller turns it into
    exit 3 — the same code, and the same shape, as `force_replay` inside `--ci` and as a secret that
    would land in a trace: contradictions are declined at the door rather than resolved by accident.
    """


def normalise(raw):
    """The mode a caller asked for, or DEFAULT when they asked for nothing.

    An unknown name is a REFUSAL rather than a fallback to the default: silently substituting a mode
    is how somebody gets frames after asking for a stream, with nothing on screen saying so.
    """
    v = (raw or "").strip().lower()
    if not v:
        return DEFAULT
    if v not in MODES:
        raise Refusal(f"unknown observation mode {v!r} — choose one of: {', '.join(MODES)}")
    return v


#: Modes that draw into the page for a PERSON, and therefore change what the run measures.
#: `record` joined `human` here with ADR-125 and NOT as a convenience: Alex's requirement of
#: 2026-08-02 is that the recording carries the cursor, because a video without one is as unreadable
#: as the bare screencast the mode exists to improve on. Drawing means slowMo and an overlay, so every
#: consequence `human` already carries — changed timing, unusable as a golden — is inherited by
#: `record` in the same breath. Derived from ONE tuple so the two cannot drift into disagreeing about
#: which run is safe to trust.
DECORATED = (HUMAN, RECORD)


def resolve(raw_mode, *, baseline=False, ci=False, heal_llm=False, vision_configured=False,
            cdp_attached=False):
    """Turn the chosen mode into a plan, or refuse.

    The refusals are the point of doing this in one place: each one needs two facts that used to live
    in different files.
    """
    mode = normalise(raw_mode)

    # ⚠ A decorated mode and a golden capture are a contradiction, not a preference. Slowing the run
    # down and drawing into the page does not DEGRADE a reference — it makes it WRONG, and wrong in a
    # way that only surfaces later, on somebody else's replay. Declined at the door.
    #
    # Walks DECORATED rather than naming `human`: when ADR-125 gave `record` a cursor it inherited
    # this contradiction whole, and a refusal that had spelled out one mode would have let the other
    # through — producing exactly the wrong-but-plausible baseline this guard was written to stop.
    if mode in DECORATED and baseline:
        raise Refusal(
            f"observe={mode} cannot be combined with a golden capture: the cursor overlay and slowMo "
            "change the very pixels and timings the baseline is being recorded to define. Capture the "
            f"baseline with observe=frames, and watch a later replay with observe={mode}")

    # ⚠ ADR-125 — an impossibility, not an unbuilt mode, and the difference decides the treatment.
    # `recordVideo` is a `newContext` option; over CDP we ADOPT a context somebody else created
    # (pw-executor/src/server.ts), so there is no creation to attach it to. `slowMo` has the same
    # shape of problem and is DEGRADED — the executor pays the pacing with its own per-step pause.
    # Video has nothing to pay with: the run would simply end with no file. So it is refused here,
    # before anything starts, and the executor carries a second, louder guard for the case where the
    # environment reached it by another route. Neither end degrades quietly.
    if mode == RECORD and cdp_attached:
        raise Refusal(
            "observe=record cannot be combined with CDP-attach (PW_CDP_ENDPOINT is set): a video is a "
            "property of a context at the moment it is CREATED, and this run adopts a context created "
            "by the browser you attached to. Nothing here can add the recording afterwards, and a run "
            "that finished without the video you asked for is not a degraded run. Record against a "
            "browser this run launches itself, or watch the live view with observe=stream")

    if mode in NOT_YET:
        raise Refusal(f"observe={mode} is not implemented in this build — {NOT_YET[mode]}")

    frames = mode != OFF

    # DOES `human` BEHAVE LIKE `stream` ON THE FRAME AXIS? Decided by reading the code rather than by
    # analogy with the docstring's "stream + cursor". The plan's frame axis expands into exactly the two
    # switches `apply` writes, and `stream` writes them exactly as `frames` does: the live screencast is
    # NOT gated by this plan at all — it is started per run by the executor's `browser.screencastStart`
    # (pw-executor/src/server.ts), which no mode here touches. So "human behaves like stream" reduces,
    # in THIS build, to "frames stay on", which `mode != OFF` already yields. DECISION: no special case
    # for `human` on the frame axis; the only thing it adds is decoration. If the screencast ever
    # becomes a switch this resolver owns, `stream` and `human` acquire it TOGETHER — one line, not two.
    #
    # Decoration is a property of the PERSON's chosen mode, not a derived state (ADR-120): it is on for
    # the decorated modes and off for everything else, and nothing else in the run may turn it on.
    decorations = mode in DECORATED

    # ADR-125. The video is the WHOLE of what `record` adds — the frame axis and the live view are
    # untouched by it, which is why `record` is not "stream plus a file". Kept as its own field rather
    # than derived at the far end from `mode`, for the same reason `decorations` is: the executor must
    # be told a decision, not left to re-make it from a string it would have to keep in sync.
    video = mode == RECORD

    if mode == OFF:
        why = "off: nothing is captured, by request"
    elif ci and mode != OFF:
        # NOT a refusal and NOT an override: `--ci` says the run is unattended, and the person may
        # still want frames from it. Saying so is enough; deciding for them would be the derivation
        # this design deliberately does not do.
        why = f"{mode}: chosen explicitly, in a --ci run where nobody is watching live"
    else:
        why = f"{mode}: chosen" + ("" if raw_mode else " by default (nothing was asked for)")

    plan = ObservationPlan(mode=mode, frames=frames, decorations=decorations, video=video, why=why)

    # The VLM layer is not a mode: it is a consequence of having a model that can read our frames. It
    # cannot turn capture ON — the person decides that — but a heal that will never receive a frame
    # must SAY so, because a heal that silently never ran is indistinguishable from one with nothing
    # to heal.
    if heal_llm and vision_configured and not frames:
        plan.why += ("; ⚠ vision heal is configured but observe=off, so it will receive no frame and "
                     "cannot look at anything")
    return plan


def apply(plan, env):
    """Expand the plan into the switches the two processes already read.

    ⚠ THE ONLY PLACE THEY ARE WRITTEN, which is the whole point. The two frame variables gate the SAME
    picture in two languages; written apart, they produce a half-observed run that reports nothing
    about the half that is missing. Written here, they cannot be set apart.

    `SENTINEL_DECORATE` (LIVE-HUMAN) and `SENTINEL_RECORD` (ADR-125) ride the SAME expansion for the
    same reason, not because they are extra frame switches: they carry `plan.decorations` and
    `plan.video` across the process boundary to their only reader, pw-executor. Expanding either
    anywhere else would put a second author on one decision, which is exactly how the original four
    switches came apart.

    ⚠ PW_NO_TRACE is not touched — see the module docstring.

    A switch the operator set BY HAND survives: this resolves a default, and overriding a default on
    a machine you own is legitimate. What it must not do is disagree with the plan it just reported,
    so the override is named in the same log line (see `overrides`).
    """
    out = dict(env)
    frames = "1" if plan.frames else "0"
    values = {"SENTINEL_LIVE_FRAMES": frames,
              "SENTINEL_TRACE_SCREENSHOTS": frames,
              "SENTINEL_DECORATE": "1" if plan.decorations else "0",
              "SENTINEL_RECORD": "1" if plan.video else "0"}
    for var in SWITCHES:
        out.setdefault(var, values[var])
    return out


def overrides(env):
    """Observation switches the caller had already set by hand, so the log can say the plan was overridden.

    Walks `SWITCHES` rather than its own list: a switch this resolver writes but does not report would
    be set by hand, silently, while the log kept claiming the plan it no longer describes.
    """
    return sorted(k for k in SWITCHES if k in env)


def from_env(env=None, *, run_mode="explore"):
    """The plan for the run this process is about to perform, from the environment it was given."""
    e = os.environ if env is None else env
    return resolve(
        e.get("SENTINEL_OBSERVE"),
        baseline=(run_mode == "baseline"),
        ci=e.get("CI", "0") == "1",
        heal_llm=e.get("HEAL_LLM", "0") == "1",
        vision_configured=(e.get("LLM_VISION") == "1" or e.get("LLM_VISION_HEAL") == "1"),
        # ADR-125. Read here rather than in `resolve` so the resolver stays a pure function of its
        # arguments, exactly like `baseline` and `ci`. `PW_CDP_ENDPOINT` is the executor's variable and
        # this file must never WRITE it — it is only asked whether the run is about to adopt somebody
        # else's browser, because that is the fact that makes a video impossible rather than merely
        # awkward. A blank value is not attachment: launch.ts trims it the same way before deciding.
        cdp_attached=bool((e.get("PW_CDP_ENDPOINT") or "").strip()),
    )
