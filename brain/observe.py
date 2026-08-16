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
frames and no reason. `record` is still in that state (LIVE-RECORD). `human` no longer is: LIVE-HUMAN
built the decoration side, so the refusal came out of `NOT_YET` IN THE SAME CHANGE as the switch that
performs it — a mode leaves the waiting room only together with the thing that does the work.

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
NOT_YET = {
    RECORD: "LIVE-RECORD brings the video artifact; recordVideo is a newContext option and the "
            "CDP-attach path adopts a context it did not create, which is the part that needs deciding",
}

#: The environment switches this resolver OWNS: `apply` writes exactly these and `overrides` reports
#: exactly these, both by walking this tuple. Two hand-kept lists is how the four switches drifted
#: apart in the first place — a switch added to one and forgotten in the other would be set silently
#: and then contradict the plan in the log without saying so.
SWITCHES = ("SENTINEL_LIVE_FRAMES", "SENTINEL_TRACE_SCREENSHOTS", "SENTINEL_DECORATE")

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
    RECORD: {"ru": "видеофайл артефактом после прогона; на живой вид не влияет",
             "en": "a video file as an artifact after the run; does not affect the live view"},
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


def resolve(raw_mode, *, baseline=False, ci=False, heal_llm=False, vision_configured=False):
    """Turn the chosen mode into a plan, or refuse.

    The refusals are the point of doing this in one place: each one needs two facts that used to live
    in different files.
    """
    mode = normalise(raw_mode)

    # ⚠ `human` and a golden capture are a contradiction, not a preference. Slowing the run down and
    # drawing into the page does not DEGRADE a reference — it makes it WRONG, and wrong in a way that
    # only surfaces later, on somebody else's replay. Declined at the door.
    if mode == HUMAN and baseline:
        raise Refusal(
            "observe=human cannot be combined with a golden capture: the cursor overlay and slowMo "
            "change the very pixels and timings the baseline is being recorded to define. Capture the "
            "baseline with observe=frames, and watch a later replay with observe=human")

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
    # `human` and off for everything else, and nothing else in the run may turn it on.
    decorations = mode == HUMAN

    if mode == OFF:
        why = "off: nothing is captured, by request"
    elif ci and mode != OFF:
        # NOT a refusal and NOT an override: `--ci` says the run is unattended, and the person may
        # still want frames from it. Saying so is enough; deciding for them would be the derivation
        # this design deliberately does not do.
        why = f"{mode}: chosen explicitly, in a --ci run where nobody is watching live"
    else:
        why = f"{mode}: chosen" + ("" if raw_mode else " by default (nothing was asked for)")

    plan = ObservationPlan(mode=mode, frames=frames, decorations=decorations, video=False, why=why)

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

    `SENTINEL_DECORATE` (LIVE-HUMAN) rides the SAME expansion for the same reason, not because it is a
    third frame switch: it carries `plan.decorations` — the person's `human` mode — across the process
    boundary to the only reader, pw-executor. Expanding it anywhere else would put a second author on
    one decision, which is exactly how the original four switches came apart.

    ⚠ PW_NO_TRACE is not touched — see the module docstring.

    A switch the operator set BY HAND survives: this resolves a default, and overriding a default on
    a machine you own is legitimate. What it must not do is disagree with the plan it just reported,
    so the override is named in the same log line (see `overrides`).
    """
    out = dict(env)
    frames = "1" if plan.frames else "0"
    values = {"SENTINEL_LIVE_FRAMES": frames,
              "SENTINEL_TRACE_SCREENSHOTS": frames,
              "SENTINEL_DECORATE": "1" if plan.decorations else "0"}
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
    )
