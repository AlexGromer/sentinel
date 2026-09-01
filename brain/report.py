"""Sentinel — run report generators (M4, ADR-014).

Pure functions over a `heal-report.json` dict: a self-contained HTML report, a machine-readable
JSON mirror, and a Prometheus textfile (`metrics.prom`, node_exporter textfile-collector format).
No browser, no external assets. The Go report-service is the eventual home post-M2b.
"""
import html
import json
import os

from .eventlog import exit_codes, log

# Цвет по РОДУ исхода (`severity`), а не по числу — ADR-141.
#
# ⚠ ЗДЕСЬ СТОЯЛО `{0: ..., 1: ..., 2: ..., 3: ...}` С ОТКАТОМ НА СЕРЫЙ `#555`, и этот откат был
# ЗНАЧЕНИЕМ ПО УМОЛЧАНИЮ У `.get`, чей ключ — `rep.get("exit_code", -1)`. То есть отчёт, который не
# удалось разобрать (кода нет вовсе → -1), и прогон, убитый сигналом (код -1), красились ОДИНАКОВО;
# туда же уезжали 4 и 5. Замерено 2026-08-31.
#
# Ключ теперь `severity` из каталога: новый код наследует цвет, объявив свой род, и ни одна запись
# не может «не попасть» в таблицу молча — `tests/test_exit_code_surfaces_offline.py` выводит нужное
# множество ключей из каталога В ОБЕ СТОРОНЫ.
_SEV_COLOR = {
    "pass": "#2e7d32",
    "finding": "#f9a825",
    "regression": "#c62828",
    "integrity": "#6a1b9a",
    "not_started": "#8d6e63",
    "tool_failure": "#c62828",
    "tool_failure_salvaged": "#e65100",
}
_UNKNOWN_COLOR = "#555"


def _exit_color(code) -> str:
    """Цвет кода выхода. Незнакомый каталогу код — серый, и это ЕДИНСТВЕННЫЙ случай серого."""
    entry = exit_codes().get(str(code))
    if not entry:
        return _UNKNOWN_COLOR
    return _SEV_COLOR.get(entry.get("severity", ""), _UNKNOWN_COLOR)


def _exit_meaning(code) -> str:
    """Английская фраза каталога для кода. Пусто — если каталог кода не объявлял.

    Отчёт англоязычный целиком (заголовки, колонки), поэтому берётся `en`, а не пара языков.
    """
    entry = exit_codes().get(str(code))
    return str(entry.get("en", "")) if entry else ""


def _metrics(rep: dict) -> str:
    lines = ["# Sentinel run metrics (Prometheus textfile format)"]
    lines.append(f"sentinel_run_steps {len(rep.get('steps', []))}")
    lines.append(f"sentinel_run_exit_code {rep.get('exit_code', -1)}")
    lines.append(f"sentinel_heal_total {rep.get('healed', 0)}")
    by_strat = {}
    for s in rep.get("steps", []):
        if s.get("outcome") == "healed":
            st = (s.get("heal") or {}).get("strategy", "unknown")
            by_strat[st] = by_strat.get(st, 0) + 1
    for st, n in sorted(by_strat.items()):
        lines.append(f'sentinel_heal_by_strategy_total{{strategy="{st}"}} {n}')
    # GAP-RISK-009/ADR-042: count by the kind label, not by exit2 — in authoritative mode a visual
    # regression has exit2=True too, so an exit2-based a11y count would misattribute it. "visual*"
    # covers both the advisory ("visual(advisory)") and authoritative ("visual") labels.
    a11y = sum(1 for g in rep.get("regressions", []) if "a11y" in g.get("kinds", []))
    visual = sum(1 for g in rep.get("regressions", []) if any(k.startswith("visual") for k in g.get("kinds", [])))
    lines.append(f'sentinel_regression_total{{kind="a11y"}} {a11y}')
    lines.append(f'sentinel_regression_total{{kind="visual"}} {visual}')
    lines.append(f"sentinel_quarantined_total {sum(1 for s in rep.get('steps', []) if s.get('quarantined'))}")
    lines.append(f"sentinel_failed_total {rep.get('failed', 0)}")
    return "\n".join(lines) + "\n"


def _html(rep: dict) -> str:
    rows = []
    for s in rep.get("steps", []):
        heal = s.get("heal") or {}
        h = (f"{heal.get('strategy', '')} ({heal.get('confidence', '')})"
             if s.get("outcome") == "healed" else "")
        reg = ",".join(s.get("regression", []))
        q = "yes" if s.get("quarantined") else ""
        rows.append(
            "<tr><td>" + html.escape(str(s.get("step_id"))) + "</td><td>"
            + html.escape(str(s.get("type"))) + "</td><td class='"
            + html.escape(str(s.get("outcome"))) + "'>" + html.escape(str(s.get("outcome")))
            + "</td><td>" + html.escape(h) + "</td><td>" + html.escape(reg)
            + "</td><td>" + q + "</td></tr>")
    code = rep.get("exit_code", -1)
    color = _exit_color(code)
    # ADR-071: the drift table. `healed N` never answered the question a reader actually has — WHAT moved
    # in the interface. Each row names the element, the class (re-bind = same element by another frozen
    # key, repairing the test; re-ground = a new selector chosen from the page as it is now, identity
    # unverified) and the before -> after locator, so "passed" can be read together with "and here is
    # what changed underneath".
    drift = rep.get("drift") or {}
    dels = drift.get("elements") or []
    drift_html = ""
    if dels:
        drows = []
        for d in dels:
            kind = d.get("kind", "")
            drows.append(
                "<tr><td>" + html.escape(str(d.get("step"))) + "</td>"
                + "<td class='" + html.escape(kind) + "'>"
                + html.escape("re-bind" if kind == "rebind" else "re-ground") + "</td>"
                + "<td>" + html.escape(str(d.get("name") or "")) + "</td>"
                + "<td>" + html.escape(str(d.get("strategy") or "")) + " ("
                + html.escape(str(d.get("confidence") or "")) + ")</td>"
                # ADR-080: WHICH heals were applied without full confidence. The data was always in the
                # artefact (`outcome` on every drift row) and no surface showed it, so a heal applied
                # optimistically looked identical to one accepted outright.
                + "<td>" + ("<span class='unverified'>"
                            + html.escape(str(d.get("outcome") or "")) + "</span>"
                            if d.get("outcome") != "auto_healed"
                            else html.escape(str(d.get("outcome") or ""))) + "</td>"
                # ADR-082: WHETHER IT IS THE SAME ELEMENT — the question the "locator: frozen -> used"
                # column raises and could not answer. A re-bind has no claim to make and shows a dash,
                # rather than an empty cell a reader would read as missing data.
                + "<td>" + (html.escape(str(d.get("identity")))
                            if d.get("identity") == "verified"
                            else ("<span class='unverified'>" + html.escape(str(d.get("identity")))
                                  + "</span>" if d.get("identity") else "&mdash;")) + "</td>"
                + "<td><code>" + html.escape(json.dumps(d.get("from"), ensure_ascii=False)) + "</code>"
                + " &rarr; <code>" + html.escape(json.dumps(d.get("to"), ensure_ascii=False))
                + "</code></td></tr>")
        note = ("" if not drift.get("failed_build") else
                " <strong>build failed on drift</strong> (threshold "
                + html.escape(str(drift.get("threshold"))) + ")")
        drift_html = (
            "<h2>Interface drift</h2><p>re-bound " + str(drift.get("rebind", 0))
            + " · re-grounded <span class='reground'>" + str(drift.get("reground", 0)) + "</span>"
            + note + "</p><table><thead><tr><th>#</th><th>class</th><th>element</th>"
            + "<th>strategy (conf.)</th><th>accepted as</th><th>identity</th>"
            + "<th>locator: frozen &rarr; used</th></tr></thead><tbody>"
            + "".join(drows) + "</tbody></table>")
    # ADR-077: what the run LOST. The codes are resolved to the catalogue's verdict sentences, not the
    # log phrasing — a reader of the report is asking "what does this mean for the result?", which is a
    # different question from "what happened at that moment", and the catalogue carries both answers.
    degr_html = ""
    degr = rep.get("degradations") or []
    if degr:
        from .eventlog import verdict_sentence
        degr_html = ("<h2>Degraded quality</h2><p>This run finished with less than it was meant to have."
                     "</p><ul>"
                     + "".join("<li><code>" + html.escape(str(c)) + "</code> — "
                               + html.escape(verdict_sentence(c)) + "</li>" for c in degr)
                     + "</ul>")
    # ADR-097: how much of the page the tool could SEE, next to the coverage figure that is measured
    # against it. Coverage answers "how much of what we saw did we exercise"; a run that perceived two
    # thirds of a screen and exercised all of it reports 1.00, and that is true — of the two thirds.
    # The reader has to be told which page the fraction is of.
    #
    # Three categories, and they SUM to the audit's own denominator. A breakdown that does not
    # decompose is decoration; this one is asserted to add up, so a category cannot quietly absorb
    # another. `opaque` is listed apart because those zones cannot be counted at all — a canvas may
    # hold one control or ten — and a guess inside the fraction would be the flattering number wearing
    # a pessimistic coat.
    perc_html = ""
    perc = rep.get("perception") or {}
    pages = perc.get("pages") or {}
    measured = {k: v for k, v in pages.items() if isinstance(v, dict) and v.get("ratio") is not None}
    if pages and not measured:
        # Said, not omitted. An absent section reads as "nothing to report", which is a different and
        # false claim from "we could not measure" (the older-executor case ADR-092 fails open into).
        perc_html = ("<h2>Page visibility</h2><p>Not measured &mdash; "
                     + html.escape(str(next(iter(pages.values()), {}).get("reason") or "no reason given"))
                     + ". Coverage below is a fraction of an unknown whole.</p>")
    elif measured:
        def _sum(f):
            return sum(int(f(v) or 0) for v in measured.values())
        usable = _sum(lambda v: v.get("usable"))
        blocked = _sum(lambda v: (v.get("blocked") or 0) + (v.get("no_role") or 0))
        unseen = _sum(lambda v: ((v.get("unseen") or {}).get("outside_selector") or 0)
                      + ((v.get("unseen") or {}).get("iframe") or 0))
        worst = perc.get("worst_ratio")
        opq = {}
        for v in measured.values():
            for k, n in (v.get("opaque") or {}).items():
                opq[k] = opq.get(k, 0) + int(n or 0)
        opq_txt = " · ".join(f"{html.escape(k.replace('_', ' '))}: {n}" for k, n in sorted(opq.items()) if n)
        perc_html = (
            "<h2>Page visibility</h2><p>The tool could see <strong>"
            + (f"{round(worst * 100)}%" if worst is not None else "?")
            + "</strong> of the least-visible page ("
            + str(len(measured)) + " measured). Coverage is a fraction of THAT.</p>"
            + "<table><thead><tr><th>controls</th><th>what it means</th></tr></thead><tbody>"
            + f"<tr><td>{usable}</td><td class='ok'>seen and usable</td></tr>"
            + f"<tr><td>{blocked}</td><td class='"
            + ("unverified" if blocked else "")
            + "'>seen, cannot act &mdash; off screen, disabled, or nothing to address it by</td></tr>"
            + f"<tr><td>{unseen}</td><td>not seen at all &mdash; outside our selector, or behind a frame"
            + " boundary</td></tr></tbody></table>"
            + (f"<p>Cannot be counted: {opq_txt}.</p>" if opq_txt else ""))

    css = ("body{font:14px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
           "td,th{border:1px solid #ddd;padding:6px 10px;text-align:left}th{background:#f5f5f5}"
           ".healed{color:#1565c0}.ok{color:#2e7d32}.failed{color:#c62828}"
           ".rebind{color:#1565c0}.reground{color:#b26a00;font-weight:700}.unverified{color:#b26a00;font-weight:700}"
           "code{background:#f5f5f5;padding:1px 4px}h2{margin-top:1.6rem;font-size:1.05rem}"
           ".exit{font-weight:700;color:" + color + "}"
           ".exitmeaning{color:" + color + ";font-weight:600}")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Sentinel report</title><style>"
        + css + "</style></head><body><h1>Sentinel run — " + html.escape(str(rep.get("mode")))
        + "</h1><p>plan: <code>" + html.escape(str(rep.get("plan_id"))) + "</code> · exit "
        # ADR-141: the number alone was the whole statement, and for 4/5/-1 it was also grey. The
        # catalogue's own sentence goes beside it — this file is the artefact a person opens when the
        # run is over and the hub is not in front of them.
        + "<span class='exit'>" + html.escape(str(code)) + "</span>"
        + (" <span class='exitmeaning'>" + html.escape(_exit_meaning(code)) + "</span>"
           if _exit_meaning(code) else "")
        + " · healed "
        + str(rep.get("healed", 0)) + " · failed " + str(rep.get("failed", 0))
        + (" · <strong>" + html.escape(str(rep.get("verdict"))) + "</strong>"
           if rep.get("verdict") else "")
        + " · regressions " + str(len(rep.get("regressions", [])))
        + "</p><table><thead><tr><th>#</th><th>type</th><th>outcome</th><th>heal</th>"
        + "<th>regression</th><th>quar.</th></tr></thead><tbody>" + "".join(rows)
        + "</tbody></table>" + perc_html + degr_html + drift_html + "</body></html>")


def push_metrics(report: dict, gateway: str, job: str = "sentinel") -> None:
    """Push the run's sentinel_* metrics to a Prometheus Pushgateway (M4b, ADR-018 — batch fits push)."""
    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
    reg = CollectorRegistry()

    def _g(name: str, val, doc: str) -> None:
        Gauge(name, doc, registry=reg).set(val)

    _g("sentinel_run_steps", len(report.get("steps", [])), "steps in the run")
    _g("sentinel_run_exit_code", report.get("exit_code", -1), "structured exit code")
    _g("sentinel_heal_total", report.get("healed", 0), "healed steps")
    _g("sentinel_failed_total", report.get("failed", 0), "failed steps")
    _g("sentinel_regression_a11y_total",
       sum(1 for x in report.get("regressions", []) if x.get("exit2")), "a11y golden regressions")
    push_to_gateway(gateway, job=job, registry=reg,
                    grouping_key={"run_id": str(report.get("plan_id", ""))})


def generate(run_dir: str) -> dict:
    """Read <run_dir>/heal-report.json and write report.json, report.html, metrics.prom, junit.xml.

    ADR-097: `plan.json` is read too, for the page-visibility block. The two artefacts are written by
    different paths — the audit runs during explore, the heal report during replay — so a report built
    from the heal report alone can never mention how much of the page was visible while it prints a
    coverage number beside it. Absent or unreadable plan.json is not an error: a replay of an imported
    plan legitimately has no audit, and the report says "not measured" rather than inventing a figure.
    """
    rep = json.loads(open(os.path.join(run_dir, "heal-report.json")).read())
    try:
        plan = json.loads(open(os.path.join(run_dir, "plan.json")).read())
        if isinstance(plan.get("perception"), dict):
            rep.setdefault("perception", plan["perception"])
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        # Said out loud rather than swallowed: a malformed plan is a fact about the run, and a report
        # that quietly omits a section is indistinguishable from a run that had nothing to report.
        log("report.perception_unreadable", error=e)
    with open(os.path.join(run_dir, "report.json"), "w") as f:
        json.dump(rep, f, indent=2)
    with open(os.path.join(run_dir, "report.html"), "w") as f:
        f.write(_html(rep))
    with open(os.path.join(run_dir, "metrics.prom"), "w") as f:
        f.write(_metrics(rep))
    # ADR-073: JUnit XML is what every CI actually consumes. Written alongside the others rather than
    # behind a flag — a reporter nobody enables is a reporter nobody has, and it costs one file.
    from .junit import to_junit
    with open(os.path.join(run_dir, "junit.xml"), "w") as f:
        f.write(to_junit(rep))
    return {"report.json": True, "report.html": True, "metrics.prom": True, "junit.xml": True}
