"""Benchmark the alt-text routes on one set of sample figures.

Each route converts every document of DOCS through `reflow.convert.convert` with the options
the CLI passes (`--alt-text local`, or `--alt-text ollama --alt-text-model TAG`), into a fresh
output folder, so `alt-text-cache.json` replays nothing. The provider that convert makes is
wrapped to time each figure; nothing else changes.

    scripts/bench_alt_text.py run ROUTE [--work DIR] [--rerun-cached]
    scripts/bench_alt_text.py run ROUTE --work DIR2 --docs SLUG ...   # add documents later
    scripts/bench_alt_text.py report [--work DIR [DIR2 ...]] [--csv PATH] [--sheet PATH]

ROUTE is `local` or `ollama:TAG`. Run one route per process, so the in-process model is freed
when the process ends. Before the documents, the route's model is loaded once and timed (the
first load): for Ollama, unload it and then an empty /api/generate; for `local`, the
model load. nvidia-smi memory.used is sampled through the run, and the peak above the idle
reading before the load is the video memory. The harness stops only the Ollama model it loaded.

`report` reads every route's run.json and output folders, and writes one row per route and
figure to a CSV, plus a grading sheet that lists each figure's drafts under shuffled letters, so
a grader does not see which route wrote which draft.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from benchlib import (  # noqa: E402
    GpuSampler,
    fresh_route,
    gpu_processes,
    idle_mib,
    ollama_host,
    ollama_load,
    ollama_loaded,
    ollama_stop,
    percentile,
    track_usage,
)

import checkout  # noqa: E402
from reflow.names import DOCUMENT_JSON, FIGURES_DIR  # noqa: E402

SAMPLES = checkout.main_checkout(REPO) / "samples"   # the gitignored samples live there

# slug -> source under samples/: the Carmel, budget and audit samples; the RFP diagrams and the
# flyer are the comparison set; the tri-fold (photos) and the NCHS chart cover the other kinds.
DOCS = {
    "da-timelines-difficult-graphics": "docaccess/11-timelines-difficult-graphics.pdf",  # Carmel
    "da-scanned-budget-handwriting": "docaccess/09-scanned-budget-handwriting.pdf",
    "da-financial-audit-comprehensive": "docaccess/07-financial-audit-comprehensive.pdf",
    "da-complex-rfp-diagrams": "docaccess/06-complex-rfp-diagrams.pdf",
    "az-accident-insurance-flyer": "azblue/site/accident-insurance-flyer.pdf",
    "cdc-antibiotics-trifold": "corpus/cdc-antibiotics-trifold.pdf",
    "nchs-bar-chart": "corpus/nchs-bar-chart.pdf",
    # the tri-fold's photos carry tagged alt, so are never drafted; these photos have none
    "cdc-firstline-slides-es": "corpus/cdc-firstline-slides-es.pdf",
}
DRAFT_WARNS = ("suggested-alt", "suggested-description", "suggested-alt-uncertain")


def run(route: str, work: Path, rerun_cached: bool, only: list[str] | None = None) -> None:
    import reflow.convert as rc
    import reflow.pipeline as rp
    from reflow.analyze import enrich, vision

    host = ollama_host()
    kind, _, tag = route.partition(":")
    if kind not in ("local", "ollama") or (kind == "ollama") != bool(tag):
        raise SystemExit("ROUTE is local or ollama:TAG")
    base, record = fresh_route(work, route)

    calls: list[dict] = []
    current = {"doc": ""}

    def timed(provider):
        describe = provider.describe
        usage = track_usage(provider.client)

        def wrapper(image, media_type, ctx):
            t0 = time.perf_counter()
            entry = {"doc": current["doc"], "page": ctx.page,
                     "sha1": hashlib.sha1(image).hexdigest()}
            try:
                result = describe(image, media_type, ctx)
                entry.update(status="ok", raw=result.__dict__)
                return result
            except vision.ProviderError as e:
                entry.update(status="failed", error=str(e)[:300])
                raise
            finally:
                entry["seconds"] = time.perf_counter() - t0
                entry["stats"] = usage["last"]
                calls.append(entry)

        provider.describe = wrapper
        return provider

    real = rp.make_provider

    def make_provider(name, *a, **kw):
        provider = real(name, *a, **kw)
        # convert also makes a "local" transcriber for --assist local; time only alt text
        return timed(provider) if name in ("local", "ollama") and current["doc"] else provider

    rp.make_provider = make_provider
    sampler = GpuSampler("memory.used", 250)
    try:
        record["idle_mib"] = idle_mib(sampler, host, tag if kind == "ollama" else None)
        t_load = time.time()
        if kind == "ollama":
            record["ollama_load_s"] = ollama_load(host, tag)
        else:
            enrich.make_provider("local").client.load()
        record["first_load_s"] = time.time() - t_load
        print(f"{route}: first load {record['first_load_s']:.1f} s", flush=True)

        docs = {}
        t_docs = time.time()
        for slug, source in DOCS.items():
            if only and slug not in only:
                continue
            current["doc"] = slug
            out = base / slug
            options = {"alt_text": kind} | ({"alt_text_model": tag} if tag else {})
            n0, t0 = len(calls), time.perf_counter()
            summary = rc.convert(SAMPLES / source, out, **options)
            docs[slug] = {"seconds": time.perf_counter() - t0, "calls": len(calls) - n0}
            print(f"{route}: {slug}: {docs[slug]['calls']} figures, "
                  f"{docs[slug]['seconds']:.1f} s", flush=True)
            del summary
        record["docs"] = docs
        t_end = time.time()
        record["peak_mib"] = max(sampler.values(t_load, t_end), default=float("nan"))
        record["peak_above_idle_mib"] = record["peak_mib"] - record["idle_mib"]
        if kind == "ollama":
            ps = ollama_loaded(host, tag) or {}
            record.update(ps_size=ps.get("size"), ps_size_vram=ps.get("size_vram"),
                          ps_context=ps.get("context_length"))
        else:
            import torch
            record["torch_max_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
        record["calls"] = calls
        record["docs_wall_s"] = t_end - t_docs

        if rerun_cached:
            n0 = len(calls)
            for slug, source in DOCS.items():
                if only and slug not in only:
                    continue
                current["doc"] = slug
                options = {"alt_text": kind} | ({"alt_text_model": tag} if tag else {})
                rc.convert(SAMPLES / source, base / slug, **options)
            record["cached_rerun_calls"] = len(calls) - n0
            record["calls"] = calls[:n0]
            print(f"{route}: cached rerun made {record['cached_rerun_calls']} model calls",
                  flush=True)
    finally:
        sampler.close()
        rp.make_provider = real
        if kind == "ollama":
            ollama_stop(host, tag)
        record["gpu_processes_after"] = gpu_processes()
        (base / "run.json").write_text(json.dumps(record, indent=1, ensure_ascii=False))


# -- report ----------------------------------------------------------------------------------


def figures(out: Path) -> list[dict]:
    """Every figure the conversion offered to the model: drafted, or still missing alt."""
    from reflow.model import Document, Role

    doc = Document.from_json((out / DOCUMENT_JSON).read_text(encoding="utf-8"))
    rows = []
    # the crops are named as enrich.describe names them: page and index among figures with a bbox
    boxed = [n for n in doc.root.walk() if n.role == Role.FIGURE and n.bbox]
    crop = {n.id: out / FIGURES_DIR / f"p{n.page:04d}-{i}.webp" for i, n in enumerate(boxed)}
    for node in boxed:
        a = node.attrs
        drafted = a.get("provenance") == "ai-drafted"
        # enrich drafts only a figure with no alt of its own (a tagged alt keeps its fact)
        missing = not a.get("alt") and any(f.code == "alt-missing" for f in node.facts)
        if not (drafted or missing):
            continue
        rows.append({"id": node.id, "page": node.page, "drafted": drafted,
                     "warn": a.get("warn", "") if drafted else "alt-missing",
                     "kind": a.get("kind", ""), "decorative": bool(a.get("decorative")),
                     "alt": a.get("alt", ""), "description": a.get("description", ""),
                     "reviewed": a.get("reviewed"), "crop": str(crop[node.id])})
    return rows


def report(works: list[Path], csv_path: Path, sheet: Path) -> None:
    """Every route's figures, merged over the work folders: a later pass with --docs may add a
    document to a route (its first load and memory are the main pass's)."""
    by_route: dict[str, list[tuple[Path, dict]]] = {}
    for work in works:
        for path in sorted(work.glob("*/run.json")):
            rec = json.loads(path.read_text())
            by_route.setdefault(rec["route"], []).append((path.parent, rec))
    table, rows, by_figure = [], [], {}
    for route, parts in by_route.items():
        main_rec = parts[0][1]
        calls = [c for _, rec in parts for c in rec.get("calls", [])]
        secs = [c["seconds"] for c in calls]
        counts = {w: 0 for w in (*DRAFT_WARNS, "alt-missing")}
        decorative = 0
        for base, _ in parts:
            for slug in DOCS:
                if not (base / slug / DOCUMENT_JSON).exists():
                    continue
                for f in figures(base / slug):
                    counts[f["warn"]] = counts.get(f["warn"], 0) + 1
                    decorative += f["decorative"]
                    rows.append({"route": route, "doc": slug, **f})
                    by_figure.setdefault((slug, f["id"], f["page"]), {})[route] = f
        table.append({
            "route": route, "figures": len(secs),
            "failed": sum(c["status"] == "failed" for c in calls),
            "median_s": statistics.median(secs) if secs else float("nan"),
            "p95_s": percentile(secs, 0.95), "total_s": sum(secs),
            "first_load_s": main_rec.get("first_load_s"),
            "peak_above_idle_mib": main_rec.get("peak_above_idle_mib"),
            "ps_size_vram": main_rec.get("ps_size_vram"),
            "torch_reserved_mib": main_rec.get("torch_max_reserved_mib"),
            "decorative": decorative, **counts,
            "cached_rerun_calls": main_rec.get("cached_rerun_calls"),
            "gpu_before": "; ".join(main_rec.get("gpu_processes_before", [])),
        })
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps(table, indent=1))
    rng = random.Random(208)
    key, lines = {}, []
    for (slug, fid, page), drafts in sorted(by_figure.items(), key=lambda kv: (kv[0][0], kv[0][2] or 0, kv[0][1])):
        routes = list(drafts)
        rng.shuffle(routes)
        crop = next(iter(drafts.values()))["crop"]
        lines.append(f"## {slug} p{page} {fid} ({crop})")
        for letter, route in zip("ABCDEFGH", routes, strict=False):
            d = drafts[route]
            key[f"{slug}|{fid}|{letter}"] = route
            lines.append(f"- {letter} [{d['warn']}, {d['kind']}] alt: {d['alt']}"
                         + (f"\n  description: {d['description']}" if d["description"] else ""))
        lines.append("")
    sheet.write_text("\n".join(lines))
    sheet.with_suffix(".key.json").write_text(json.dumps(key, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("route")
    r.add_argument("--work", type=Path, default=Path("/tmp/altbench"))
    r.add_argument("--rerun-cached", action="store_true")
    r.add_argument("--docs", nargs="*", help="only these slugs (a trial run)")
    p = sub.add_parser("report")
    p.add_argument("--work", type=Path, nargs="+", default=[Path("/tmp/altbench")])
    p.add_argument("--csv", type=Path, default=Path("/tmp/altbench/figures.csv"))
    p.add_argument("--sheet", type=Path, default=Path("/tmp/altbench/sheet.md"))
    args = parser.parse_args()
    if args.cmd == "run":
        run(args.route, args.work, args.rerun_cached, args.docs)
    else:
        report(args.work, args.csv, args.sheet)


if __name__ == "__main__":
    main()
