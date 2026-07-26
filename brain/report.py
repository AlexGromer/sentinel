"""Sentinel — run report generators (M4, ADR-014).

Pure functions over a `heal-report.json` dict: a self-contained HTML report, a machine-readable
JSON mirror, and a Prometheus textfile (`metrics.prom`, node_exporter textfile-collector format).
No browser, no external assets. The Go report-service is the eventual home post-M2b.
"""
import html
import json
import os

_EXIT_COLOR = {0: "#2e7d32", 1: "#f9a825", 2: "#c62828", 3: "#6a1b9a"}


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
    color = _EXIT_COLOR.get(code, "#555")
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
            + "<th>strategy (conf.)</th><th>locator: frozen &rarr; used</th></tr></thead><tbody>"
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
    css = ("body{font:14px system-ui;margin:2rem}table{border-collapse:collapse;width:100%}"
           "td,th{border:1px solid #ddd;padding:6px 10px;text-align:left}th{background:#f5f5f5}"
           ".healed{color:#1565c0}.ok{color:#2e7d32}.failed{color:#c62828}"
           ".rebind{color:#1565c0}.reground{color:#b26a00;font-weight:700}"
           "code{background:#f5f5f5;padding:1px 4px}h2{margin-top:1.6rem;font-size:1.05rem}"
           ".exit{font-weight:700;color:" + color + "}")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Sentinel report</title><style>"
        + css + "</style></head><body><h1>Sentinel run — " + html.escape(str(rep.get("mode")))
        + "</h1><p>plan: <code>" + html.escape(str(rep.get("plan_id"))) + "</code> · exit "
        + "<span class='exit'>" + html.escape(str(code)) + "</span> · healed "
        + str(rep.get("healed", 0)) + " · failed " + str(rep.get("failed", 0))
        + (" · <strong>" + html.escape(str(rep.get("verdict"))) + "</strong>"
           if rep.get("verdict") else "")
        + " · regressions " + str(len(rep.get("regressions", [])))
        + "</p><table><thead><tr><th>#</th><th>type</th><th>outcome</th><th>heal</th>"
        + "<th>regression</th><th>quar.</th></tr></thead><tbody>" + "".join(rows)
        + "</tbody></table>" + degr_html + drift_html + "</body></html>")


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
    """Read <run_dir>/heal-report.json and write report.json, report.html, metrics.prom, junit.xml."""
    rep = json.loads(open(os.path.join(run_dir, "heal-report.json")).read())
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
