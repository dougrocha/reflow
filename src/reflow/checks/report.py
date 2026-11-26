"""The audit report (checks.source) as Markdown or as a standalone HTML page.

Both say the same things in the same order: the overall result, the SRC-* table, and the
veraPDF section when the audit ran veraPDF. The status is a word, never only a color.
"""

from __future__ import annotations

from markupsafe import Markup

from reflow.checks.source import AuditReport


def _summary(report: AuditReport) -> str:
    failed = sum(f.status == "fail" for f in report.findings)
    text = (f"{failed} of {len(report.findings)} checks failed." if failed
            else f"No check failed ({len(report.findings)} checks).")
    if report.verapdf is not None:
        text += f" veraPDF: {report.verapdf.status}."
    return text


def _md_cell(text: str) -> str:
    return " ".join(text.split()).replace("\\", "\\\\").replace("|", "\\|")


def to_markdown(report: AuditReport) -> str:
    lines = [f"# Accessibility audit: {_md_cell(report.filename)}", "",
             f"**{'Failed' if report.failed else 'Passed'}.** {_summary(report)}", "",
             "| ID | Check | WCAG | Matterhorn | Status | Detail |",
             "| --- | --- | --- | --- | --- | --- |"]
    for f in report.findings:
        c = f.check
        lines.append(f"| {c.id} | {_md_cell(c.title)} | {c.wcag} | {c.matterhorn or '-'} "
                     f"| {f.status} | {_md_cell(f.detail)} |")
    vera = report.verapdf
    if vera is not None:
        lines += ["", "## veraPDF (PDF/UA-1)", "", f"**{vera.status}**: {_md_cell(vera.detail)}"]
        if vera.rules:
            lines += ["", "| Clause | Test | Failed checks | Rule |", "| --- | --- | --- | --- |"]
            lines += [f"| {r.clause} | {r.test} | {r.failed_checks} | {_md_cell(r.description)} |"
                      for r in vera.rules]
    return "\n".join(lines) + "\n"


_CSS = """
body { font: 16px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 72rem;
       padding: 0 1rem; color: #1a1a1a; background: #fff; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #999; padding: .4rem .6rem; text-align: left; vertical-align: top; }
thead th { background: #eee; }
.status { font-weight: 600; white-space: nowrap; }
.fail { color: #a10000; } .pass { color: #0b5d1e; } .warn { color: #6b4b00; }
.skip { color: #4a4a4a; }
"""


def _row(cells: list) -> Markup:
    out = Markup("")
    for i, cell in enumerate(cells):
        out += (Markup('<th scope="row">{}</th>') if i == 0
                else Markup("<td>{}</td>")).format(cell)
    return Markup("<tr>{}</tr>\n").format(out)


def _status(status: str) -> Markup:
    return Markup('<span class="status {0}">{0}</span>').format(status)


def _head(*names: str) -> Markup:
    cells = Markup("").join(Markup('<th scope="col">{}</th>').format(n) for n in names)
    return Markup("<thead><tr>{}</tr></thead>").format(cells)


def to_html(report: AuditReport) -> str:
    rows = Markup("")
    for f in report.findings:
        c = f.check
        rows += _row([c.id, c.title, c.wcag, c.matterhorn or "-", _status(f.status), f.detail])
    body = Markup(
        "<h1>Accessibility audit: {name}</h1>\n<p><strong>{result}.</strong> {summary}</p>\n"
        "<table>\n<caption>Source checks (SPEC §7)</caption>\n{head}\n<tbody>\n{rows}</tbody>\n"
        "</table>\n").format(
            name=report.filename, result="Failed" if report.failed else "Passed",
            summary=_summary(report),
            head=_head("ID", "Check", "WCAG", "Matterhorn", "Status", "Detail"), rows=rows)
    vera = report.verapdf
    if vera is not None:
        body += Markup("<h2>veraPDF (PDF/UA-1)</h2>\n<p>{}: {}</p>\n").format(
            _status(vera.status), vera.detail)
        if vera.rules:
            vrows = Markup("").join(_row([r.clause, r.test, str(r.failed_checks), r.description])
                                    for r in vera.rules)
            body += Markup("<table>\n<caption>Failed rules</caption>\n{}\n<tbody>\n{}</tbody>\n"
                           "</table>\n").format(
                _head("Clause", "Test", "Failed checks", "Rule"), vrows)
    return str(Markup(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Accessibility audit: {name}</title>\n<style>{css}</style>\n</head>\n"
        "<body>\n<main>\n{body}</main>\n</body>\n</html>\n").format(
            name=report.filename, css=Markup(_CSS), body=body))

