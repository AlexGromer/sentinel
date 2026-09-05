"""Offline gate: the capabilities catalogue cannot advertise what the product lacks (PROD-DISCOVERY).

Run:  .venv/bin/python tests/test_capabilities_offline.py

docs/capabilities.json is the catalogue of features that exist and work but were unreachable because
nothing named them (the LiteLLM class of gap: code present, cannot be found). A capabilities page is
worse than useless if it drifts from reality — a reader who follows a listed feature to a command
that no longer exists trusts the page less than if it had said nothing.

So every entry carries an ACCESS ref, and this gate verifies that ref RESOLVES in the real code:
  cli     -> a subcommand present in cmd/agentctl/main.go's switch
  http    -> a route registered via HandleFunc in cmd/control-api
  mode    -> a RUN_MODE value the brain dispatches on
  profile -> a docker-compose profile
  service -> a docker-compose service in the DEFAULT stack (started by `docker compose up`)
  env     -> an environment variable read by non-test product code
  code    -> a token present in a named source file
  file    -> a path that exists
  ui      -> a hub view: `data-view="<ref>"` in docs/index.html AND `<ref>` in the hub's own VIEWS

⚠ `ui` СТОЯЛО В ТЕЛЕ ГЕЙТА И ОТСУТСТВОВАЛО В ЭТОМ ПЕРЕЧНЕ (16 употреблений в каталоге, вторая по
частоте разновидность после `cli`), а шапка самого `docs/capabilities.json` не называла ни `ui`, ни
`service`. То есть ОБА описания видов доступа — и docstring гейта, и заголовок каталога — протухли,
и каждое по-своему: читатель, сверявшийся с любым из них, считал бы живой вид доступа несуществующим.
Ровно тот класс, что уже записан в этом дереве: докстринг — ЗАЯВЛЕНИЕ О ПОКРЫТИИ, и оно бывает ложным.

This is behavioural, not a claim about the page's prose: rename a subcommand, drop a route, remove a
profile, and the gate breaks instead of the page misleading a reader.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _read_glob(reldir, suffix):
    out = []
    for root, _dirs, files in os.walk(os.path.join(REPO, reldir)):
        if "node_modules" in root:
            continue
        for f in files:
            if f.endswith(suffix) and not f.endswith("_test.go"):
                out.append(os.path.join(root, f))
    return out


def _resolve(cid, kind, ref, agentctl, control_api, brain_main, compose, product_src, file_ref=None):
    """Resolve ONE access path against the real code. Extracted so a capability can declare several.

    Unchanged in substance from when each entry had exactly one path — the checks are the same, they
    are simply applied per path now.
    """

    if kind == "cli":
        # TWO shapes are a CLI path, and both are real. A `case "x":` in main.go's switch is the
        # hand-written subcommand; a row in `apiVerbs` (cmd/agentctl/api.go) is the table-driven
        # projection of a control-API route — `agentctl runs artifact` is as much a terminal path as
        # `agentctl import`. A gate that knew only the switch called every projected verb missing the
        # moment the capability list started deriving them.
        api = _read(os.path.join("cmd", "agentctl", "api.go"))
        assert f'case "{ref}":' in agentctl or f'Verb: "{ref}"' in api, (
            f"{cid}: cli path {ref!r} is neither a subcommand in cmd/agentctl/main.go's switch nor a "
            f"verb in cmd/agentctl/api.go's apiVerbs table")
    elif kind == "http":
        # Two shapes register a route: the declaration table in access.go (ADR-109 second half,
        # where the mux is built from `{pattern: "GET /v1/x", …}`) and the direct HandleFunc the
        # UI-mode routes still use. A gate that knew only the second one reported every route
        # missing the moment the table arrived.
        assert (f'HandleFunc("{ref}"' in control_api or f'{{pattern: "{ref}"' in control_api), (
            f"{cid}: HTTP route {ref!r} is not registered in cmd/control-api")
    elif kind == "mode":
        # the brain must dispatch on this RUN_MODE, not merely mention it in a comment: require the
        # quoted string to appear in an equality/branch context.
        assert (f'"{ref}"' in brain_main), f"{cid}: RUN_MODE {ref!r} not found in brain/__main__.py"
        assert re.search(rf'run_mode\s*==\s*"{re.escape(ref)}"|"{re.escape(ref)}"\s*[:,)]|_run_{ref.replace("-", "_")}',
                         brain_main), (
            f"{cid}: RUN_MODE {ref!r} appears but not in a dispatch position — is it really handled?")
    elif kind == "profile":
        assert f'profiles: ["{ref}"]' in compose, (
            f"{cid}: docker-compose profile {ref!r} does not exist")
    elif kind == "service":
        # A capability of the DEFAULT stack: `docker compose up` and it is there. Both halves
        # matter. Existence alone would keep passing if someone put the service back behind a
        # profile, and then the catalogue would promise a thing the documented command does not
        # start — the precise failure this catalogue exists to prevent, one level up.
        m = re.search(rf"(?m)^  {re.escape(ref)}:\s*$", compose)
        assert m, f"{cid}: docker-compose service {ref!r} does not exist"
        tail = compose[m.end():]
        nxt = re.search(r"(?m)^  [a-z0-9][\w-]*:\s*$|^[a-zA-Z_][\w-]*:\s*$", tail)
        body = tail[: nxt.start()] if nxt else tail
        assert not re.search(r"(?m)^    profiles:", body), (
            f"{cid}: service {ref!r} is behind a profile, so `docker compose up` does not start "
            f"it and the catalogue's access path is a flag the reader was not told to pass")
    elif kind == "env":
        assert ref in product_src, (
            f"{cid}: env var {ref!r} is read by no non-test product code — dead or renamed?")
    elif kind == "code":
        src = _read(file_ref)
        assert ref in src, f"{cid}: token {ref!r} not found in {file_ref}"
    elif kind == "file":
        assert os.path.exists(os.path.join(REPO, ref)), f"{cid}: file {ref!r} does not exist"
    elif kind == "ui":
        # A `ui` ref names a VIEW of the hub, and the catalogue offers a button that navigates to
        # it. The ref must therefore resolve to a real view — otherwise the button is a promise
        # the page cannot keep, which is the exact failure the catalogue exists to prevent, moved
        # from prose into a control. Both halves are required: the view must EXIST as a
        # data-view section, and setView must accept it (VIEWS is what it validates against).
        hub = _read(os.path.join("docs", "index.html"))
        assert f'data-view="{ref}"' in hub or f'data-view="{ref} ' in hub, (
            f"{cid}: ui view {ref!r} has no data-view section in docs/index.html")
        assert re.search(rf"VIEWS\s*=\s*\[[^\]]*'{re.escape(ref)}'", hub) or \
               re.search(rf'VIEWS\s*=\s*\[[^\]]*"{re.escape(ref)}"', hub), (
            f"{cid}: ui view {ref!r} is not in VIEWS, so setView would refuse to open it")
    else:
        raise AssertionError(f"{cid}: unknown access.kind {kind!r}")


def main() -> int:
    cat = json.loads(_read(os.path.join("docs", "capabilities.json")))
    caps = cat["capabilities"]
    assert caps, "capabilities.json parsed to zero entries — this gate would be vacuous"

    agentctl = _read(os.path.join("cmd", "agentctl", "main.go"))
    control_api = "\n".join(_read(os.path.relpath(p, REPO)) for p in _read_glob("cmd/control-api", ".go"))
    brain_main = _read(os.path.join("brain", "__main__.py"))
    compose = _read("docker-compose.yml")
    # product code the env/code checks may search (Go + Python + TS, no tests)
    product_files = (_read_glob("cmd", ".go") + _read_glob("brain", ".py")
                     + _read_glob("pw-executor/src", ".ts"))
    product_files = [p for p in product_files if not p.endswith(".test.ts")]
    product_src = "\n".join(_read(os.path.relpath(p, REPO)) for p in product_files)

    seen_ids = set()
    for c in caps:
        cid = c["id"]
        assert cid not in seen_ids, f"duplicate capability id {cid!r}"
        seen_ids.add(cid)
        for k in ("title_ru", "title_en", "how_ru", "how_en"):
            assert c.get(k), f"{cid}: missing {k}"
        # ACCESS IS A LIST (2026-08-07). It used to be one object, and a single path can only ever
        # answer "reachable somewhere" — which was read as "covered". Measured before the change: of
        # 50 entries, ZERO could be shown reachable all three ways, because the SHAPE could not
        # express it. Every listed path is resolved below, exactly as the single one used to be.
        paths = c["access"]
        assert isinstance(paths, list) and paths, (
            f"{cid}: access must be a non-empty LIST of paths — one path per surface")
        assert c.get("reach") in ("capability", "artifact"), (
            f"{cid}: missing `reach` — a product CAPABILITY is expected to be reachable three ways, "
            f"an ARTIFACT (Helm chart, env knob, compose profile) is not, and the difference has to "
            f"be stated rather than inferred from the kind")
        for acc in paths:
            kind, ref = acc["kind"], acc["ref"]
            _resolve(cid, kind, ref, agentctl, control_api, brain_main, compose, product_src,
                     acc.get("file"))
        continue

    # ---- THE PRINCIPLE, AS A NUMBER (2026-08-07) -------------------------------------------------
    #
    # "Everything three ways" was a belief, not a measurement: the registry held ONE path per entry,
    # so it answered "reachable somewhere" and was read as "covered". Measured at the moment the shape
    # changed: of 33 product capabilities, ZERO could be shown reachable from the UI, a terminal AND
    # over HTTP — not because they are not, but because nothing recorded it.
    #
    # A gate that DEMANDED all three today would either stall this change or produce three dozen
    # hastily invented reasons, which is worse than none. So it RATCHETS: the number is printed every
    # run and may not fall. Raising the floor is a deliberate edit that says "this many are now
    # genuinely reachable three ways", which is the only claim worth trusting.
    THREE = {"ui", "cli", "http"}
    MIN_THREE_WAY = 13         # ⚠ may only ever go UP; today's honest number.
    #                            12 -> 13 at ADR-152: `goal-reached` ships with all three surfaces
    #                            from the start (terminal run line · artifact over HTTP · the hub's
    #                            Results view), so the floor rises WITH the feature rather than
    #                            later. Raising it here is the cheap half; the expensive half —
    #                            that this gate asserts a COUNT and not a SHARE, so a one-surface
    #                            capability would have passed green — is [CAPABILITIES-THREE-WAY-
    #                            NOT-GATED], fixed in its own PR with its own mutations.
    #                            6 -> 12 at ADR-149, the largest single move this ratchet has made,
    #                            and it is a CATALOGUE change rather than new product surface: six
    #                            capabilities were written as TWO records each — a "server" one and a
    #                            "UI" one — so neither half could ever satisfy a rule about one
    #                            capability being reachable three ways. The paths existed; the file's
    #                            shape hid them. Merging the pairs made five of them three-way at
    #                            once, `promote-test` a sixth once it regained the UI path that used
    #                            to live on the deleted `ui-library`.
    #                            ⚠ MIN_CAPABILITIES below did NOT have to move, and the registry
    #                            entry said it would: it called the floor "satisfied only just" after
    #                            the merge. Measured — 38 capabilities against a floor of 30, a slack
    #                            of 8. The claim was arithmetic from an older population.
    #                            5 -> 6 at ADR-146, and unlike the catch-up below this is a NEW
    #                            claim rather than a correction: `provider-keys` is three-way from
    #                            the day it exists — the settings panel, `agentctl provider-keys
    #                            set`, and PUT /v1/provider-keys — because the completeness gate in
    #                            cmd/agentctl (ADR-107) refused the HTTP routes until the terminal
    #                            path existed. The floor moves in the SAME commit as the capability:
    #                            a ratchet raised later is a ratchet that would not have noticed the
    #                            surface never arriving.
    #                            HEALTH-006 PR-B moved it off ZERO for the first time in the
    #                            project's life: readiness is reachable as GET /readyz, as
    #                            `agentctl health`, and as the `health` view. The number was 0
    #                            not because nothing was reachable three ways, but because the
    #                            registry could not SAY so until it held every path per entry.
    #                            1 -> 4 at ADR-126, and the edit is a CATCH-UP rather than a new
    #                            claim: `live-per-run`, `observe-mode` and `health-readiness` had
    #                            been three-way for some time, and `run-video` joined at ADR-125,
    #                            while the floor sat where HEALTH-006 left it. A ratchet that lags
    #                            the truth is a ratchet that would not notice a fall back to it.
    # ПЕРЕЧЕНЬ ВИДОВ ДОСТУПА ВЫВОДИТСЯ ИЗ ЗАПИСЕЙ И СВЕРЯЕТСЯ С ОБОИМИ ОПИСАНИЯМИ.
    #
    # ⚠ ЗАВЕДЕНО ПОТОМУ, ЧТО ОБА ОПИСАНИЯ УЖЕ ПРОТУХЛИ, И КАЖДОЕ ПО-СВОЕМУ (замерено 2026-09-05):
    # шапка `docs/capabilities.json` называла СЕМЬ видов и не знала ни `service`, ни `ui`; докстринг
    # ЭТОГО файла называл ВОСЕМЬ и не знал `ui`. При этом `ui` — вторая по частоте разновидность в
    # каталоге (16 употреблений), и обе она обходила стороной. Разовая правка обоих списков не
    # лечит класс: через месяц появится десятый вид, и всё повторится. Поэтому истина берётся из
    # ЗАПИСЕЙ, а описания обязаны её покрывать — рукописный список показывает лишнее, но не
    # показывает пропущенное, потому что у отсутствия нет представления, на которое можно смотреть
    # (`docs/DEVELOPMENT.md` §0, принцип 5).
    kinds_used = {a["kind"] for c in caps for a in c["access"]}
    header_kinds = set(re.findall(r"^\s{2}([a-z]+)\s+:", "\n".join(cat.get("_", [])), re.M))
    doc_kinds = set(re.findall(r"^\s{2}([a-z]+)\s+->", __doc__ or "", re.M))
    assert kinds_used <= header_kinds, (
        f"docs/capabilities.json header does not declare access kind(s) {sorted(kinds_used - header_kinds)}, "
        f"which entries actually use — a reader checking against the header would call a working "
        f"access path non-existent")
    assert kinds_used <= doc_kinds, (
        f"this gate's own docstring does not name access kind(s) {sorted(kinds_used - doc_kinds)}, "
        f"which entries actually use. A docstring is a CLAIM ABOUT COVERAGE and it can be false — "
        f"already measured twice in this repository")

    # ⚠ ЗДЕСЬ СТОЯЛО `assert MIN_THREE_WAY >= 0`, И ЭТО БЫЛА ЕДИНСТВЕННАЯ ПРОВЕРКА САМОГО ПОЛА.
    # «Ратчет только растёт» жило КОММЕНТАРИЕМ, а не утверждением: ничто в дереве не сравнивало пол
    # с прошлым значением, поэтому регресс чинился правкой числа — ровно тем способом, который
    # соседняя строка объявляла недопустимым. ЗАМЕРЕНО 2026-09-05 на настоящем гейте: понижение
    # 13 → 11 прошло ЗЕЛЁНЫМ.
    #
    # ЧТО ЗАМЕРЕНО ЕЩЁ, И ЭТО ХУЖЕ. Утверждался СЧЁТ три-способных, а не ДОЛЯ, поэтому новая
    # возможность с ОДНИМ путём доступа проходила зелёной: `caps_only` 39 → 40, `three_way`
    # остаётся 13, оба `>=` выполнены, а печатаемая доля молча ухудшается (замерено: 13/40).
    # То есть гейт, заведённый ради принципа «каждая возможность достижима тремя способами», был
    # зелёным ровно над его нарушением.
    #
    # ЛЕЧЕНИЕ — РАВЕНСТВО, А НЕ ПОРОГ, и обе стороны сразу. Число три-способных и число НЕ
    # три-способных фиксируются ТОЧНО, поэтому любое движение каталога — и потеря поверхности, и
    # появление недостижимой возможности — требует ОСОЗНАННОЙ правки числа в ТОМ ЖЕ коммите, где
    # менялся каталог, и правка видна в диффе строкой. Порог этого не даёт: он пропускает движение
    # «в хорошую сторону» молча, а именно там и прячется разбавление доли.
    THREE_WAY_TODAY = 13       # ⚠ равенство, не порог: менять ТОЛЬКО вместе с каталогом и с причиной
    NOT_THREE_WAY_TODAY = 26   # ⚠ и это тоже равенство — иначе доля падает молча
    MIN_CAPABILITIES = 30      # a floor on the walk itself: classifying everything as `artifact`
    #                            would make the ratchet vacuous, so the population is bounded too
    caps_only = [c for c in caps if c.get("reach") == "capability"]
    three_way = [c for c in caps_only if {p["kind"] for p in c["access"]} >= THREE]
    partial = [c for c in caps_only if not ({p["kind"] for p in c["access"]} >= THREE)]

    assert len(caps_only) >= MIN_CAPABILITIES, (
        f"only {len(caps_only)} entries are classified as a product capability — the rest as artifacts. "
        f"That makes the three-way ratchet measure almost nothing; check the `reach` values.")
    assert MIN_THREE_WAY == THREE_WAY_TODAY, (
        f"the historical ratchet floor ({MIN_THREE_WAY}) and today's recorded number "
        f"({THREE_WAY_TODAY}) disagree — they describe the same fact and must move together")
    assert len(three_way) == THREE_WAY_TODAY, (
        f"three-way reachability is {len(three_way)}, recorded as {THREE_WAY_TODAY}. "
        f"If it FELL, a capability lost a surface: {[c['id'] for c in partial][:8]}. If it ROSE, say "
        f"so by editing the number in this commit — a ratchet that lags the truth would not notice a "
        f"fall back to it.")
    assert len(partial) == NOT_THREE_WAY_TODAY, (
        f"{len(partial)} capabilities are short of three ways, recorded as {NOT_THREE_WAY_TODAY}. "
        f"A NEW capability that ships fewer than three surfaces lands here — give it the missing "
        f"paths, or raise this number deliberately with the reason next to it. Asserting the count "
        f"of three-way entries alone would have passed this silently, which is measured, not feared.")

    # Every capability short of three ways must SAY which surface is missing, or the gap is invisible
    # again.
    #
    # ⚠ ПРЕДИКАТ БЫЛ НЕВЕРЕН, И ОШИБАЛСЯ В СТОРОНУ СНИСХОЖДЕНИЯ. Хвост `and not c.get("missing")`
    # означал, что запись, объявившая ХОТЬ ОДНУ дыру, считается объяснённой — даже когда дыр две.
    # Замерено 2026-09-05: `openai-shim`, `import-http` и `revisions-http` имели дыры {cli, ui} и
    # объявляли только `cli`, то есть печатаемое число было занижено на три (19 вместо 22) и
    # занижалось ТЕМ СИЛЬНЕЕ, чем аккуратнее выглядела запись. Теперь объяснённой считается только
    # та, у которой объявлены ВСЕ дыры.
    #
    # ⚠ И ЭТО БЫЛ `print`, А НЕ `assert`. Требование «назови дыру» вычислялось и печаталось —
    # то есть его нельзя было нарушить. Теперь это ратчет вниз: число неназванных дыр может только
    # УБЫВАТЬ. Требовать ноль сегодня нельзя (их 19), и гейт, который стоит красным с первого дня,
    # выключают в первую неделю — но каждая новая запись обязана называть свои дыры, иначе число
    # вырастет и гейт покраснеет.
    def _holes_unnamed(c):
        """Остались ли у записи НЕОБЪЯВЛЕННЫЕ дыры. ВСЕ дыры, а не хотя бы одна."""
        return not (THREE - {p["kind"] for p in c["access"]}) <= set(c.get("missing", {}))

    # ⚠ ПРАВИЛО ПРИБИТО ОТДЕЛЬНО ОТ КАТАЛОГА, И ЭТО ОТВЕТ НА ВЫЖИВШУЮ МУТАЦИЮ. Замерено: возврат
    # предиката к прежней снисходительной форме (`… and not c.get("missing")`) проходил ЗЕЛЁНЫМ —
    # не потому, что правило не важно, а потому, что после починки трёх записей каталог стал чистым
    # и обе формы давали одно число. То есть правило проверялось ТОЛЬКО через сегодняшние данные и
    # исчезло бы вместе с ними. Здесь оно проверяется на синтетике — ТОЙ ЖЕ функцией, которой
    # считается каталог, а не её копией.
    _two_holes = {"access": [{"kind": "http", "ref": "x"}], "missing": {"cli": "есть причина"}}
    assert _holes_unnamed(_two_holes), (
        "a capability with holes {cli, ui} that names only `cli` must count as UNEXPLAINED — naming "
        "one hole of two is how a gap stays invisible while looking documented")
    _two_holes_named = {"access": [{"kind": "http", "ref": "x"}],
                        "missing": {"cli": "причина", "ui": "причина"}}
    assert not _holes_unnamed(_two_holes_named), "naming every hole must count as explained"
    _all_three = {"access": [{"kind": k, "ref": "x"} for k in THREE]}
    assert not _holes_unnamed(_all_three), "a three-way capability has no holes to name"

    unexplained = [c["id"] for c in partial if _holes_unnamed(c)]
    MAX_UNEXPLAINED = 19       # ⚠ может только УБЫВАТЬ; сегодняшнее честное число
    assert len(unexplained) <= MAX_UNEXPLAINED, (
        f"{len(unexplained)} capabilities name no missing surface (recorded ceiling "
        f"{MAX_UNEXPLAINED}): {sorted(unexplained)[:8]}. A capability short of three ways must say "
        f"WHICH surface it lacks — an unnamed gap is invisible, and invisible is how it stays.")
    print(f"    three ways: {len(three_way)}/{len(caps_only)} capabilities; "
          f"{len(unexplained)} name no missing surface at all (потолок {MAX_UNEXPLAINED})")

    # The high-severity features the audit called out by name must be present — a catalogue that
    # quietly dropped the Helm chart or the OpenAI shim would pass every per-entry check above while
    # failing the reader who came looking for exactly those.
    must_have = {"openai-shim", "helm-chart", "cdp-attach", "takeover", "mcp-server",
                 "airgap-bundle", "login-as-test", "install-sh", "export-spec"}
    missing = must_have - seen_ids
    assert not missing, f"the catalogue is missing high-severity capabilities: {sorted(missing)}"

    # The catalogue itself must be reachable, or it is one more undiscoverable feature. Both the
    # README and the published landing page must link to CAPABILITIES.md — the two front doors a new
    # user actually opens.
    readme = _read("README.md")
    assert "docs/CAPABILITIES.md" in readme, "README does not link to docs/CAPABILITIES.md — the catalogue is itself undiscoverable"
    landing = _read(os.path.join("docs", "index.html"))
    assert "CAPABILITIES.md" in landing, "the landing page (docs/index.html) does not link to CAPABILITIES.md"

    # And the prose pages exist in both languages (bilingual parity is enforced separately, but a
    # missing English mirror here would ship a half-built catalogue).
    for page in ("docs/CAPABILITIES.md", "docs/CAPABILITIES.en.md"):
        assert os.path.exists(os.path.join(REPO, page)), f"missing prose page {page}"

    print(f"capabilities: OK ({len(caps)} entries, all access paths resolve; "
          f"{sum(1 for c in caps if c.get('severity') == 'high')} high-severity; README+landing link it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
