"""Command-line interface."""

from __future__ import annotations

import dataclasses
import inspect as pyinspect
import json
import sys
import threading
import time
import typing
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Annotated

import pikepdf
import typer
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.progress import Progress
from rich.table import Table
from typer.core import TyperCommand

from reflow import budget, progress_lines
from reflow.analyze.vision import ProviderUnavailable
from reflow.checks.doctor import probes
from reflow.checks.report import to_html as audit_html
from reflow.checks.report import to_markdown as audit_markdown
from reflow.checks.source import audit as run_audit
from reflow.convert import DocResult, Summary, convert_many
from reflow.convert import convert as run_convert
from reflow.ingest.ocr import OcrUnavailable
from reflow.ingest.pages import ingest
from reflow.model import Document
from reflow.names import PROPOSAL_DECISIONS_JSON, PROPOSALS_JSON
from reflow.prepare import RestrictedPdf, SourceUnreadable
from reflow.render.pagenames import page_count_text
from reflow.run import FLAG, Options, OptionsError
from reflow.steps import STEPS

app = typer.Typer(no_args_is_help=True, help="Turn PDFs into accessible HTML.")
err = Console(stderr=True)


class PasswordCommand(TyperCommand):
    """Typer's option parser requires a value even with prompt_required=False.

    Normalize a bare --password to an empty value and remember its intent in the
    invocation context. Explicit empty passwords remain distinct from a prompt.
    """
    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        args = list(args)
        options = {o for p in self.get_params(ctx) for o in (*p.opts, *p.secondary_opts)} | {"--"}
        for i, arg in enumerate(args):
            if arg == "--":
                break
            following = args[i + 1].split("=", 1)[0] if i + 1 < len(args) else "--"
            if arg == "--password" and following in options:
                args[i] = "--password="
                ctx.meta["prompt_pdf_password"] = True
        return super().parse_args(ctx, args)


def _password(ctx: typer.Context, value: str) -> str:
    if ctx.meta.get("prompt_pdf_password"):
        return typer.prompt("PDF password", hide_input=True, default="", show_default=False)
    source = ctx.get_parameter_source("password")
    if source is not None and source.name == "COMMANDLINE":
        err.print("Warning: --password <value> is visible in the process list. "
                  "Use REFLOW_PDF_PASSWORD or --password without a value.")
    return value


PasswordOpt = Annotated[str, typer.Option(
    "--password", envvar="REFLOW_PDF_PASSWORD", callback=_password,
    help="Encrypted PDF password: REFLOW_PDF_PASSWORD or --password for a hidden prompt. "
         "An explicit value is visible in the process list.")]


def _unreadable(pdf: Path, error: BaseException | None) -> typer.Exit:
    """The message and exit code for the error of opening `pdf`: 2 for a password it needs, 1
    for a file that is not a PDF or cannot be repaired."""
    if isinstance(error, pikepdf.PasswordError):
        err.print(f"[red]{pdf} is encrypted; pass --password.[/red]")
        return typer.Exit(2)
    err.print(f"[red]Could not open {pdf}: {error}[/red]")
    return typer.Exit(1)


@contextmanager
def _pdf_errors(pdf: Path) -> Iterator[None]:
    """Turn the errors of opening `pdf` into a message and an exit code (`_unreadable`). Wrap
    only the step that opens the file: a PDF library error later in a run is a bug."""
    try:
        yield
    except (pikepdf.PasswordError, pikepdf.PdfError) as e:
        raise _unreadable(pdf, e) from None


class JsonProgress:
    """`--progress-json`: a conversion's progress as JSON lines on stdout, throttled to about
    four a second per output folder, plus the first of every stage and the one that reaches its
    total. Called from the threads of convert-many."""

    def __init__(self) -> None:
        self._last: dict[Path, tuple[str, int, float]] = {}
        self._lock = threading.Lock()

    def __call__(self, out_dir: Path, stage: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        with self._lock:
            prev = self._last.get(out_dir)
            finished = total is not None and done >= total
            if prev and prev[0] == stage and (prev[1] == done or (not finished and now - prev[2] < 0.25)):
                return
            self._last[out_dir] = (stage, done, now)
            print(progress_lines.progress_line(str(out_dir), stage, done, total), flush=True)


def _ok_detail(summary: Summary) -> str:
    """The details of a document's `ok` line (convert-many, rerender --progress-json)."""
    return (f"{page_count_text(summary.pages)}, {summary.figures} figures, "
            f"{summary.tables} tables, {sum(summary.seconds.values()):.0f}s"
            + (f", model: {summary.model}" if summary.model else ""))


def _stage_times(summary: Summary, floor: float = 0.1) -> str:
    """The stages of a conversion that took `floor` seconds or more, in order, on one line
    ("extract 0.9s, publish 1s"). Empty when none did."""
    return ", ".join(
        f"{stage} {seconds:.1f}s" + (f" ({summary.gpu_wait[stage]:.1f}s on the GPU lease)"
                                     if summary.gpu_wait.get(stage, 0) >= floor else "")
        for stage, seconds in summary.seconds.items() if seconds >= floor)


@app.command(cls=PasswordCommand)
def inspect(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    password: PasswordOpt = "",
    as_json: Annotated[bool, typer.Option("--json", help="Print the document model as JSON.")] = False,
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Write JSON to a file.")] = None,
) -> None:
    """Read a PDF's metadata and classify each page."""
    with _pdf_errors(pdf), Progress(console=err, transient=True) as progress:
        task = progress.add_task("Reading pages", total=None)
        doc = ingest(pdf, password,
                     on_page=lambda done, total: progress.update(task, completed=done,
                                                                 total=total))

    if output:
        output.write_text(doc.to_json(), encoding="utf-8")
        err.print(f"Wrote {output}")
    if as_json:
        print(doc.to_json())
    elif not output:
        _print_summary(doc)


def _declared(field: dataclasses.Field) -> pyinspect.Parameter:
    """The typer parameter of an Options field declared with `option`: its type and default from
    the field, its help from its Flag."""
    flag = field.metadata[FLAG]
    kind, default = typing.get_type_hints(Options)[field.name], field.default
    if isinstance(default, tuple):   # page_widths: "800,1200" on the command line
        kind, default = str, ",".join(map(str, default))
    keywords = {"help": flag.help, **flag.args, **({"min": flag.min} if flag.min is not None else {})}
    flags = [] if flag.pair else ["--" + field.name.replace("_", "-")]
    return pyinspect.Parameter(field.name, pyinspect.Parameter.POSITIONAL_OR_KEYWORD, default=default,
                             annotation=Annotated[kind, typer.Option(*flags, **keywords)])


def declared_params(command: str) -> list[pyinspect.Parameter]:
    """The parameters `command` takes from Options, in field order."""
    return [_declared(f) for f in dataclasses.fields(Options)
            if FLAG in f.metadata and command in f.metadata[FLAG].commands]


def with_options(command: str, after: str):
    """Give the command function (whose `**options` collects them) the typer parameters of every
    Options field declared for `command`, placed after the parameter `after`."""
    def decorate(func):
        hints = typing.get_type_hints(func, include_extras=True)
        params = [p.replace(annotation=hints.get(p.name, p.annotation))
                  for p in pyinspect.signature(func).parameters.values()
                  if p.kind is not pyinspect.Parameter.VAR_KEYWORD]
        at = next(i for i, p in enumerate(params) if p.name == after) + 1
        params[at:at] = declared_params(command)
        func.__signature__ = pyinspect.Signature(params)
        func.__annotations__ = {p.name: p.annotation for p in params}
        return func
    return decorate


def option_of(name: str):
    """The typer annotation of one Options field, for a command that takes just that setting."""
    return _declared(next(f for f in dataclasses.fields(Options) if f.name == name)).annotation


DecisionsDirOpt = Annotated[Path | None, typer.Option(
    "--decisions-dir", file_okay=False,
    help="Off by default. A folder of decisions files: each document reads "
         "<folder>/<its output folder name>.jsonl. See `reflow convert --decisions`.")]

StageCacheOpt = option_of("stage_cache")


def _convert_options(options: dict) -> dict:
    """The keywords of `convert` for the settings typer parsed from the command line."""
    return {**options, "page_widths": parse_widths(options["page_widths"])}


def parse_widths(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split(","))
    except ValueError:
        raise typer.BadParameter(f"{text!r} is not a comma-separated list of widths") from None


JobsOpt = Annotated[int, typer.Option(
    "--jobs", "-j", min=1,
    help="Worker processes for text extraction, table finding and page images (default: CPU "
         "count minus 2, at least 1). 1 runs everything in this process. Every conversion "
         "on the machine shares one budget of workers (REFLOW_WORKER_BUDGET, default CPU count "
         "minus 2; 0 turns it off): a worker holds a slot only while it runs a task, so with "
         "others running this gets its fair share or fewer, and more when they finish.")]

AuditJobsOpt = Annotated[int, typer.Option(
    "--jobs", "-j", min=1,
    help="Worker processes that parse the pages of a long tag tree (default: CPU count minus 2, "
         "at least 1), within the machine's worker budget. The report is the same for any count.")]


def _transcription(a: dict, out_dir: Path) -> str:
    return (f"AI transcription ({a['provider']}): {a['drafted']} drafted, "
            f"{a['cached']} from cache, {a['rejected']} rejected by the checks, "
            f"{a['failed']} failed (see review.json)")


def _structure_assist(a: dict, out_dir: Path) -> str:
    return (f"AI structure assist ({a['model']}): {a['accepted']} lines turned into "
            f"paragraphs, {a['confirmed']} headings kept, {a['none']} unsure, "
            f"{a['rejected']} rejected by the page, {a['cached']} from cache, "
            f"{a['calls']} calls, {a['failed']} failed, {a['skipped']} left to the rules "
            "(see review.json)")


def _scan_fields(a: dict, out_dir: Path) -> str:
    if a["mode"] == "assist":
        return (f"AI scan-field assist ({a['model']}): {a['injected']} fields on "
                f"{a['pages']} scanned pages, {a['certain']} sure from the rules, "
                f"{a['accepted']} accepted, {a['none']} none, {a['dropped']} printed text, "
                f"{a['rejected']} rejected by the page, {a['cached']} from cache, "
                f"{a['calls']} calls, {a['failed']} failed, {a['skipped']} left out "
                "(see review.json)")
    left = f"; {a['doubtful']} doubtful places left out (--assist-scan-fields asks the model)" \
        if a["doubtful"] else ""
    broke = f"; {a['errors']} pages skipped after an error (see review.json)" if a.get("errors") else ""
    return (f"Scan fields: {a['injected']} fields on {a['pages']} scanned pages "
            f"(see review.json){left}{broke}")


def _label_assist(a: dict, out_dir: Path) -> str:
    return (f"AI label assist ({a['model']}): {a['accepted']} captions chosen, "
            f"{a['confirmed']} rules' guesses kept, {a['none']} none, {a['rejected']} rejected by the page, "
            f"{a['cached']} from cache, {a['calls']} calls, {a['failed']} failed, "
            f"{a['skipped']} left to the rules (see review.json)")


def _page_vision(a: dict, out_dir: Path) -> str:
    waiting = (f"; {a['pending']} waiting for review (reflow review {out_dir})"
               if a["pending"] else "")
    return (f"AI page check: {a['pages']} pages read ({a['cached']} from cache, "
            f"{a['calls']} calls, {a['failed']} failed, {a['answers_rejected']} answers "
            f"rejected, {a['skipped']} skipped); {a['proposals']} heading proposals: "
            f"{a['applied']} applied, {a['rejected']} rejected, {a['stale']} stale"
            f"{waiting}")


def _direct(a: dict, out_dir: Path) -> str:
    unread = f", {len(a['unread'])} left unread" if a["unread"] else ""
    stop = " (stopped at the spending cap)" if a["stopped"] else ""
    return (f"Direct transcription ({a['model']}): {a['read']} pages read, "
            f"{a['cached']} from cache{unread}{stop}; {a['tokens_in']} tokens in, "
            f"{a['tokens_out']} out, ${a['usd']:.4f} for these pages, "
            f"${a['spent']:.4f} spent this run")


def _polish(a: dict, out_dir: Path) -> str:
    unread = f", {len(a['unread'])} left unread" if a["unread"] else ""
    return (f"AI polish ({a['model']}): {a['read']} pages read, {a['cached']} from "
            f"cache{unread}; {a['applied']} edits applied, {a['rejected']} rejected; "
            f"${a['usd']:.4f} for these pages, ${a['spent']:.4f} spent this run")


def _form_repair(a: dict, out_dir: Path) -> str:
    return (f"AI form repair ({a['model']}): {a['regions']} unread regions, "
            f"{a['proposed']} readings for review, {a['unresolved']} unresolved, "
            f"{a['skipped']} skipped; {a['calls']} calls, {a['cached']} from cache, "
            f"{a['failed']} failed (see review.json)")


# One report line per AI step (reflow.steps.STEPS), from the step's stats and the output folder.
REPORTS: dict[str, Callable[[dict, Path], str]] = {
    "assist": _transcription, "label_assist": _label_assist, "structure_assist": _structure_assist,
    "scan_fields": _scan_fields, "direct": _direct, "polish": _polish, "page_vision": _page_vision,
    "form_repair": _form_repair,
}


def report(summary: Summary, out_dir: Path, console: Console, err: Console) -> None:
    """Print one conversion's Summary (the lines in design.md's Usage). Reads Summary
    attributes only, so a missing field is an AttributeError; a line whose value is zero or
    empty is left out."""
    s = summary
    console.print(f"[bold]{s.title}[/bold]: {page_count_text(s.pages)} → {out_dir}/index.html "
                  f"(engine: {s.engine})")
    suspect = ", suspect" if s.title_suspect else ""
    colour = "red" if s.title_suspect else "default"
    console.print(f"[{colour}]Title: {s.title} (from {s.title_origin}{suspect})[/{colour}]")
    if s.device:
        console.print(f"Device: {s.device}")
    for note in s.device_notes:
        err.print(f"[yellow]{note}[/yellow]")
    if s.ocr_missing:
        console.print(f"[red]{s.ocr_missing} image-only pages without text[/red] "
                      "(no OCR engine; see reflow doctor)")
    if s.docling_pages:
        reasons = Counter(r for rs in s.docling_pages.values() for r in rs)
        console.print(f"Docling ran on {len(s.docling_pages)} of {page_count_text(s.pages)} ("
                      + ", ".join(f"{k} {v}" for k, v in reasons.most_common()) + ")")
    if s.srcset_cutoff:
        console.print(f"Page images: {s.base_width} px only (document over "
                      f"{page_count_text(s.srcset_cutoff)}; --srcset-max-pages to change)")
    console.print(f"{s.chunks} chunks, {s.headings} headings, {s.figures} figures, "
                  f"{s.tables} tables; index.html {s.index_bytes // 1024} KB, "
                  f"largest chunk {s.largest_chunk_bytes // 1024} KB")
    a = s.alt_text
    if s.figures and a["provider"] != "none":
        console.print(f"Suggested alt text ({a['provider']}): {a['described']} drafted, "
                      f"{a['cached']} from cache, {a['decorative']} marked decorative, "
                      f"{a['failed']} failed, {a.get('skipped', 0)} skipped as needing no "
                      "description")
    for e in s.alt_text_errors[:5]:
        err.print(f"[yellow]  {e}[/yellow]")
    for step in STEPS:
        if stats := getattr(s, step.key):
            console.print(REPORTS[step.key](stats, out_dir))
    r = s.review
    if r["errors"] or r["warnings"]:
        errors = f"[red]{r['errors']} errors[/red]" if r["errors"] else "0 errors"
        warnings = f"[yellow]{r['warnings']} warnings[/yellow]" if r["warnings"] else "0 warnings"
        console.print(f"{errors}, {warnings} on {page_count_text(r['pages'])} (see {out_dir}/review.json)")
    if s.decisions:
        a = s.decisions
        console.print(f"decisions: {a['applied']} applied, {a['dismissed']} dismissed, "
                      f"{a['orphaned']} orphaned (see {out_dir}/decisions.json)")
    if s.model:
        console.print(f"Model: {s.model}")
    if s.seconds:
        console.print("Time: " + ", ".join(
            f"{k} {v}s" + (f" ({s.gpu_wait[k]}s waiting for the GPU)" if k in s.gpu_wait else "")
            for k, v in s.seconds.items()))


@app.command(cls=PasswordCommand)
@with_options("convert", after="password")
def convert(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("-o", "--output", help="Output directory.")] = Path("out"),
    password: PasswordOpt = "",
    jobs: JobsOpt = budget.cpu_slots(),
    **options,
) -> None:
    """Convert a PDF into an accessible HTML site in the output directory."""
    try:
        with Progress(console=err, transient=True) as progress, budget.lease() as lease:
            tasks: dict[str, int] = {}

            def update(stage: str, done: int, total: int | None) -> None:
                if stage not in tasks:
                    tasks[stage] = progress.add_task(stage, total=total)
                progress.update(tasks[stage], completed=done, total=total)

            # The pool's tasks hold slots of the machine's worker budget only while they run.
            summary = run_convert(pdf, output, password=password, jobs=lease.cap(jobs),
                                  lease=lease, on_progress=update, **_convert_options(options))
    except SourceUnreadable as e:
        raise _unreadable(pdf, e.__cause__) from None
    except RestrictedPdf as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(3)
    except (ProviderUnavailable, OcrUnavailable, OptionsError) as e:   # what the caller asked for
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    # Anything else is a bug in the run: it surfaces with its traceback and a non-zero exit.
    report(summary, output, Console(), err)


@app.command()
def rerender(
    out_dir: Annotated[Path, typer.Argument(file_okay=False, help="A conversion's output directory.")],
    decisions: Annotated[Path, typer.Option(
        "--decisions", dir_okay=False,
        help="The reviewer's decisions file (JSONL, docs/review-decisions.md) to apply.")],
    stage_cache: StageCacheOpt = None,
    source: Annotated[Path | None, typer.Option(
        "--source", dir_okay=False,
        help="Run only if a full conversion of this PDF, with --engine and --alt-text (their "
        "defaults when not given), is the conversion recorded; else exit 4.")] = None,
    engine: Annotated[str | None, typer.Option(
        "--engine", help="With --source: the engine a full conversion would use.")] = None,
    alt_text: Annotated[str | None, typer.Option(
        "--alt-text", help="With --source: the alt-text provider a full conversion would use.")] = None,
    progress_json: Annotated[bool, typer.Option(
        "--progress-json", help="Print `start`, then `ok` or `FAILED`, and progress as JSON "
        "lines on stdout, as convert-many does.")] = False,
    jobs: JobsOpt = budget.cpu_slots(),
) -> None:
    """Apply a decisions file to a conversion without converting it again.

    Replays the file on the model the conversion saved (the stage cache, with --stage-cache on
    the conversion) and renders the site again, with the options the conversion recorded. It
    runs only when a full conversion would build the same model: the same converter code, tool
    versions, options and PDF. Else it changes nothing and exits 4, naming what moved.
    Exits 2 when OUT_DIR holds no conversion or the decisions file is not readable."""
    from reflow.rerender import CannotRerender, NoConversion, rerender as replay
    emit = JsonProgress()

    def say(status: str, out: str, details: str) -> None:
        if progress_json:
            print(progress_lines.status_line(status, out, details), flush=True)

    def on_progress(stage: str, done: int, total: int | None) -> None:
        if progress_json:
            emit(out_dir, stage, done, total)

    say(progress_lines.START, str(out_dir), out_dir.name)
    try:
        with budget.lease() as lease:
            expected = {k: v for k, v in (("engine", engine), ("alt_text", alt_text)) if v}
            summary = replay(out_dir, decisions, stage_cache=stage_cache,
                             jobs=lease.cap(jobs), lease=lease, on_progress=on_progress,
                             source=source, options=expected)
    except (NoConversion, ValueError) as e:
        say(progress_lines.FAILED, str(out_dir), f"{type(e).__name__}: {e}")
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    except CannotRerender as e:
        say(progress_lines.FAILED, str(out_dir), f"{progress_lines.CANNOT_RERENDER}: {e}")
        err.print(f"[red]Cannot apply the decisions without a full conversion ({e}).[/red]")
        raise typer.Exit(4)
    except Exception as e:  # noqa: BLE001 - reported on the progress channel, then raised
        say(progress_lines.FAILED, str(out_dir), f"{type(e).__name__}: {e}")
        raise
    if progress_json:
        print(progress_lines.timings_line(str(out_dir), summary.seconds, summary.gpu_wait), flush=True)
    say(progress_lines.OK, str(out_dir), _ok_detail(summary))
    report(summary, out_dir, Console(), err)


@app.command()
def review(
    out_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False,
                                            help="A conversion's output directory.")],
    accept: Annotated[list[str] | None, typer.Option(
        "--accept", help="Accept this proposal (repeatable).")] = None,
    reject: Annotated[list[str] | None, typer.Option(
        "--reject", help="Reject this proposal (repeatable).")] = None,
) -> None:
    """List the page-vision proposals of a conversion, or accept or reject them.

    A decision is kept in the output directory and carried from one conversion to the next.
    The next `reflow convert --assist-page-vision` into the same directory applies the accepted
    proposals, each only if the document's text is still the text you reviewed."""
    from reflow.analyze import page_vision as pv
    from reflow.analyze import proposals as pr
    from reflow.analyze.decisions import Verdict

    try:
        listed = pv.read_proposals(out_dir / PROPOSALS_JSON)
        decisions = dict(pr.load_decisions(out_dir / PROPOSAL_DECISIONS_JSON).by_id)
    except ValueError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    by_id = {p.id: p for p in listed}
    asked = [(pid, Verdict.ACCEPT) for pid in accept or []] + \
        [(pid, Verdict.REJECT) for pid in reject or []]
    unknown = [pid for pid, _ in asked if pid not in by_id]
    twice = {pid for pid, _ in asked if sum(1 for q, _ in asked if q == pid) > 1}
    if unknown or twice:
        if unknown:
            err.print(f"[red]No such proposal: {', '.join(unknown)}[/red]")
        if twice:
            err.print(f"[red]Decided more than once: {', '.join(sorted(twice))}[/red]")
        raise typer.Exit(2)
    if asked:
        for pid, verdict in asked:
            p = by_id[pr.ProposalId(pid)]
            decisions[p.id] = pr.Decided(verdict, p.document_text)
        pr.save_decisions(out_dir / PROPOSAL_DECISIONS_JSON, pr.Decisions(decisions))
        crops = any(d.verdict is Verdict.ACCEPT and pid in by_id
                    and by_id[pid].change is pr.Change.UNREAD_CROP
                    for pid, d in decisions.items())   # every saved accept, not only these
        flags = "--assist-page-vision" + (" --assist-page-vision-unread" if crops else "")
        Console().print(f"{len(asked)} decisions saved. Convert again with {flags} "
                        f"into {out_dir} to apply the accepted ones.")
        return
    table = Table("page", "change", "text", "model read", "status", "decision")
    table.add_column("id", no_wrap=True, min_width=15)
    for p in listed:
        decided = decisions.get(p.id)
        read = p.word.value.replace("_", " ") + (f"; and {p.doubt}" if p.doubt else "")
        table.add_row(str(p.page), p.change.value, p.text, read,
                      p.status.value, decided.verdict.value if decided else "", p.id)
    Console().print(table)


@app.command("convert-many", cls=PasswordCommand)
@with_options("convert-many", after="max_docs")
def convert_many_cmd(
    pairs: Annotated[list[Path], typer.Argument(
        help="PDF and output directory, alternating: <pdf> <out> [<pdf> <out> ...].")],
    password: PasswordOpt = "",
    jobs: JobsOpt = budget.cpu_slots(),
    max_docs: Annotated[int, typer.Option(
        min=1, help="Documents converted at the same time.")] = 2,
    decisions_dir: DecisionsDirOpt = None,
    progress_json: Annotated[bool, typer.Option(
        "--progress-json", help="Also print progress as JSON lines on stdout, for a program "
        "to read: {\"event\": \"progress\", \"out\": <output directory>, \"stage\": ..., "
        "\"done\": n, \"total\": n or null}. At most about four a second per document, plus "
        "the first of every stage and the one that reaches its total. The other output is unchanged.")] = False,
    **options,
) -> None:
    """Convert several PDFs at once on one shared process pool.

    Prints one `start`, then one `ok` or `FAILED` line per document (tab-separated: status,
    output directory, details); one failing document does not stop the others. Exits 1 if any
    failed. The output of each is the same as `reflow convert --jobs 1` of that PDF."""
    if len(pairs) % 2 or not pairs:
        err.print("[red]Give PDF and output directory in pairs.[/red]")
        raise typer.Exit(2)
    docs = list(zip(pairs[0::2], pairs[1::2]))
    for pdf, _ in docs:
        if not pdf.is_file():
            err.print(f"[red]{pdf}: no such file.[/red]")
            raise typer.Exit(2)
    lock = threading.Lock()
    stages: dict[int, str] = {}
    emit = JsonProgress()

    def say(status: str, out: str, details: str) -> None:
        with lock:
            print(progress_lines.status_line(status, out, details), flush=True)

    def on_progress(i: int, stage: str, done: int, total: int | None) -> None:
        if stages.get(i) != stage:  # one line per stage change, not per page
            stages[i] = stage
            with lock:
                print(f"  {docs[i][1].name}: {stage}", file=sys.stderr, flush=True)
        if progress_json:
            emit(docs[i][1], stage, done, total)

    def on_done(i: int, r: DocResult) -> None:
        if r.ok and (summary := r.summary) is not None:
            if progress_json:
                with lock:
                    print(progress_lines.timings_line(str(r.out_dir), summary.seconds,
                                                      summary.gpu_wait), flush=True)
            if times := _stage_times(summary):
                with lock:
                    print(f"  {r.out_dir.name}: {times}", file=sys.stderr, flush=True)
            say(progress_lines.OK, str(r.out_dir), _ok_detail(summary))
        else:
            say(progress_lines.FAILED, str(r.out_dir), f"{type(r.error).__name__}: {r.error}")
            with lock:   # stderr: stdout holds only the protocol lines
                print(r.traceback_text, file=sys.stderr, end="", flush=True)

    with budget.lease() as lease:   # the shared pool, within the machine's worker budget
        # Its tasks take slots as they run (convert_many, `lease`), so it starts at full size and
        # a later run still gets its share. A budget of 1 is serial.
        results = convert_many(docs, jobs=lease.cap(jobs),
                               lease=lease, max_docs=max_docs,
                               on_start=lambda i: say(progress_lines.START, str(docs[i][1]), docs[i][0].name),
                               on_done=on_done, on_progress=on_progress, password=password,
                               decisions_dir=decisions_dir, **_convert_options(options))
    failed = sum(not r.ok for r in results)
    if failed:
        err.print(f"[red]{failed} of {len(results)} documents failed.[/red]")
        raise typer.Exit(1)


def _print_summary(doc: Document) -> None:
    m = doc.meta
    console = Console()

    def yes_no(ok: bool) -> str:
        return "[green]yes[/green]" if ok else "[red]no[/red]"

    def value(v: object) -> str:
        return str(v) if v else "[red]missing[/red]"

    table = Table(title=m["filename"], show_header=False, title_justify="left")
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Title", value(m["title"]))
    table.add_row("Language", value(m["lang"]))
    table.add_row("Pages", str(m["page_count"]))
    table.add_row("PDF version", m["pdf_version"])
    table.add_row("Producer", m["producer"] or "-")
    table.add_row("Tagged", yes_no(m["tagged"]))
    table.add_row("Structure tree", yes_no(m["has_struct_tree"]))
    table.add_row("Shows title in window", yes_no(m["display_doc_title"]))
    table.add_row("Encrypted", "yes" if m["encrypted"] else "no")
    table.add_row("Assistive tech allowed", yes_no(m["allows_accessibility"]))
    if m["repair_warnings"]:
        table.add_row("Repair warnings", f"[yellow]{m['repair_warnings']}[/yellow]")
    console.print(table)

    kinds = Counter(p.kind.value for p in doc.pages)
    console.print("Pages by kind: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))


_STATUS_STYLE = {"pass": "green", "fail": "red", "warn": "yellow", "skip": "dim"}


class AuditFormat(str, Enum):
    text = "text"
    json = "json"
    markdown = "markdown"
    html = "html"


@app.command(cls=PasswordCommand)
def audit(
    pdf: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    password: PasswordOpt = "",
    as_json: Annotated[bool, typer.Option("--json", help="Same as --format json.")] = False,
    fmt: Annotated[AuditFormat, typer.Option(
        "--format", help="How to print the report.")] = AuditFormat.text,
    verapdf: Annotated[bool, typer.Option(
        "--verapdf", help="Also validate against PDF/UA-1 with veraPDF (on PATH) and merge its "
                          "result into the report.")] = False,
    jobs: AuditJobsOpt = budget.cpu_slots(),
) -> None:
    """Check the source PDF against the SRC-* table (SPEC §7). Exits 1 when a check fails, or
    when --verapdf finds the file not PDF/UA-1 compliant."""
    report = run_audit(pdf, password, jobs=jobs, verapdf=verapdf)   # within the worker budget
    if as_json:
        fmt = AuditFormat.json
    if fmt is AuditFormat.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif fmt is AuditFormat.markdown:
        print(audit_markdown(report), end="")
    elif fmt is AuditFormat.html:
        print(audit_html(report), end="")
    else:
        console = Console()
        table = Table(title=report.filename, title_justify="left")
        table.add_column("ID", style="bold", no_wrap=True)
        table.add_column("Check")
        table.add_column("WCAG", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Detail")
        for f in report.findings:
            style = _STATUS_STYLE[f.status]
            table.add_row(f.check.id, f.check.title, f.check.wcag,
                          f"[{style}]{f.status}[/{style}]", f.detail)
        console.print(table)
        failed = sum(f.status == "fail" for f in report.findings)
        if failed:
            console.print(f"[red]{failed} of {len(report.findings)} checks failed.[/red]")
        else:
            console.print(f"[green]No check failed[/green] ({len(report.findings)} checks).")
        if (vera := report.verapdf) is not None:
            style = _STATUS_STYLE[vera.status]
            console.print(f"veraPDF (PDF/UA-1): [{style}]{vera.status}[/{style}], "
                          f"{escape_markup(vera.detail)}")
            for r in vera.rules:
                console.print(f"  {r.clause}-{r.test} ({r.failed_checks} checks): "
                              f"{escape_markup(r.description)}")
    if report.failed:
        raise typer.Exit(1)


@app.command()
def doctor() -> None:
    """Report which optional tools this machine has (OCR, docling, CUDA, chromium)."""
    console = Console()
    table = Table(title="reflow doctor", title_justify="left")
    table.add_column("Tool", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Needed for")
    table.add_column("Detail")
    styles = {"ok": "green", "missing": "yellow", "broken": "red"}
    for p in probes():
        style = styles[p.status]
        table.add_row(p.name, f"[{style}]{p.status}[/{style}]", p.needed_for, p.detail)
    console.print(table)
