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

⚠ MODES THIS BUILD CANNOT DO ARE REFUSED, NOT FAKED. `human` (synthetic cursor, slowMo, highlight)
and `record` (video artifact) are declared here because the set is the product's answer to "what can
I ask for", but their machinery arrives with LIVE-HUMAN and LIVE-RECORD. Until then, asking for one
is refused with the task named. Accepting a mode and quietly doing something else is the class of
silence this whole arc exists to remove — a person who asked to watch would get frames and no reason.
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
    HUMAN: "LIVE-HUMAN brings the synthetic cursor, slowMo and highlight; Playwright draws no cursor "
           "at all and clicks are instantaneous, so this mode has to be BUILT rather than switched on",
    RECORD: "LIVE-RECORD brings the video artifact; recordVideo is a newContext option and the "
            "CDP-attach path adopts a context it did not create, which is the part that needs deciding",
}

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
    if mode == OFF:
        why = "off: nothing is captured, by request"
    elif ci and mode != OFF:
        # NOT a refusal and NOT an override: `--ci` says the run is unattended, and the person may
        # still want frames from it. Saying so is enough; deciding for them would be the derivation
        # this design deliberately does not do.
        why = f"{mode}: chosen explicitly, in a --ci run where nobody is watching live"
    else:
        why = f"{mode}: chosen" + ("" if raw_mode else " by default (nothing was asked for)")

    plan = ObservationPlan(mode=mode, frames=frames, decorations=False, video=False, why=why)

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

    ⚠ THE ONLY PLACE THEY ARE WRITTEN, which is the whole point. Both variables gate the SAME picture
    in two languages; written apart, they produce a half-observed run that reports nothing about the
    half that is missing. Written here, they cannot be set apart.

    ⚠ PW_NO_TRACE is not touched — see the module docstring.

    A switch the operator set BY HAND survives: this resolves a default, and overriding a default on
    a machine you own is legitimate. What it must not do is disagree with the plan it just reported,
    so the override is named in the same log line (see `overrides`).
    """
    out = dict(env)
    frames = "1" if plan.frames else "0"
    out.setdefault("SENTINEL_LIVE_FRAMES", frames)
    out.setdefault("SENTINEL_TRACE_SCREENSHOTS", frames)
    return out


def overrides(env):
    """Observation switches the caller had already set by hand, so the log can say the plan was overridden."""
    return sorted(k for k in ("SENTINEL_LIVE_FRAMES", "SENTINEL_TRACE_SCREENSHOTS") if k in env)


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
