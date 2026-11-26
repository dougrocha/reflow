"""Benchmark the local Ollama models for the AI assist: speed, energy, VRAM, errors, accuracy.

One harness for every model. It asks every question of the label eval (fw9, I-9, DocAccess
appeal form) and of the structure eval (appeal form, I-9, five AZ Blue forms) straight through
`OllamaChoiceProvider.choose`, with the crops and candidates the converter produces, so the
timing is the model's and not the conversion's. It never touches the replay cache.

    scripts/bench_assist_models.py extract [--work DIR]    # convert once, keep the questions
    scripts/bench_assist_models.py run [--work DIR] [--runs 5] [--model M ...]
    scripts/bench_assist_models.py report [--work DIR] [--csv-prefix PATH] [--record-caches]

`run` interleaves the models: for run r, each model in an order rotated by r is unloaded, then
loaded by one discarded warm-up question (the cold load), then asked every question while
nvidia-smi power.draw and memory.used are sampled. Results are appended to DIR/results.jsonl
(a finished model/run is skipped on a rerun). Needs a running `ollama serve`; stops nothing
but the models it unloads with `ollama stop`.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import pickle
import random
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchlib  # noqa: E402
import eval_label_assist as ela  # noqa: E402
import eval_structure_assist as esa  # noqa: E402

import checkout  # noqa: E402
from reflow import providers  # noqa: E402
from reflow.analyze import ask  # noqa: E402
from reflow.analyze import label_assist as la  # noqa: E402
from reflow.analyze import structure_assist as sa  # noqa: E402
from reflow.analyze.vision import ProviderError, ProviderUnavailable, Reply  # noqa: E402
from reflow.convert import convert  # noqa: E402
from reflow.model import Document  # noqa: E402
from reflow.names import DOCUMENT_JSON  # noqa: E402

MODELS = ["qwen3-vl:8b", "gemma4:12b", "gemma4:e4b", "ornith-1.5:9b", "bonsai-2:9b",
          "clef-flash:9b", "d1:3b"]
KWH_PRICE = 0.15            # dollars per kWh, the assumed electricity price
TIMEOUT = 180.0
SAMPLE_SEED = 20261021   # the fixed sample of questions asked again for determinism
REPEATS = 10
SERVE_LOG = Path("/tmp/bench/serve.log")


# -- extract ---------------------------------------------------------------------------------


class Capture:
    """A provider that records what the converter would send and answers "changes nothing"."""
    def __init__(self, cache_id: str, neutral: str) -> None:
        self.cache_id, self.neutral, self.items = cache_id, neutral, []

    def choose(self, question, image, crop):
        self.items.append((question, image, crop))
        return Reply(self.neutral)


def extract(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    label_forms = json.loads(ela.LABELS.read_text())["forms"]
    structure_forms = json.loads(esa.LABELS.read_text())["forms"]
    items, docs = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for kind, forms in (("label", label_forms), ("structure", structure_forms)):
            for form, entry in forms.items():
                pdf = checkout.sample(REPO, entry["pdf"])
                rules = Path(tmp) / f"{kind}-{form}-rules"
                convert(pdf, rules, page_images=False, jobs=1)
                text = (rules / DOCUMENT_JSON).read_text()
                rules_doc = Document.from_json(text)
                sha = str(rules_doc.meta.get("sha256", ""))
                docs[(kind, form)] = {"sha": sha, "json": text}
                cap = Capture("capture", "none" if kind == "label" else "unsure")
                opts = ({"assist_labels": True, "assist_labels_max_calls": 1000} if kind == "label"
                        else {"assist_structure": True, "assist_structure_max_calls": 1000})
                convert(pdf, Path(tmp) / f"{kind}-{form}-cap", page_images=False, jobs=1,
                        label_provider=cap, assist_model="capture", **opts)
                mod = la if kind == "label" else sa
                expect = {mod.cache_key(sha, q, "m") for q in mod.questions(rules_doc)}
                got = {mod.cache_key(sha, q, "m") for q, _, _ in cap.items}
                if expect != got:
                    print(f"WARNING {kind}:{form}: converter asked {len(got)} questions, the "
                          f"output document yields {len(expect)}; {len(got & expect)} in common",
                          file=sys.stderr)
                for q, image, crop in cap.items:
                    items.append({"id": f"{kind}:{form}:{q.node}", "kind": kind, "form": form,
                                  "sha": sha, "question": q, "image": image, "crop": crop})
    (work / "questions.pkl").write_bytes(pickle.dumps({"items": items, "docs": docs}))
    by_kind = defaultdict(int)
    for it in items:
        by_kind[it["kind"]] += 1
    print(f"extracted {len(items)} questions {dict(by_kind)} from {len(docs)} conversions "
          f"into {work / 'questions.pkl'}")


def load_questions(work: Path) -> dict:
    return pickle.loads((work / "questions.pkl").read_bytes())


# -- measuring -------------------------------------------------------------------------------


def api(path: str, body: dict | None = None) -> dict:
    return benchlib.ollama_api(benchlib.ollama_host(), path, body)


def unload_all() -> None:
    benchlib.ollama_stop(benchlib.ollama_host())


def served_requests() -> int:
    """POSTs to the two inference endpoints in the server log so far (proof the work ran)."""
    try:
        text = SERVE_LOG.read_text(errors="replace")
    except OSError:
        return -1
    return sum(1 for line in text.splitlines()
               if "[GIN]" in line and "POST" in line
               and ('"/api/chat"' in line or '"/v1/systemone"' in line))


def classify(error: Exception) -> str:
    message = str(error)
    if "timed out" in message:
        return "timeout"
    if "answered" in message:
        return "http_error"
    return "malformed"


def put_question(provider: ask.OllamaChoiceProvider, item: dict) -> dict:
    started = time.perf_counter()
    out = {"id": item["id"], "answer": None, "confidence": None, "status": "ok", "error": "",
           "stats": {}}
    try:
        reply = provider.choose(item["question"], item["image"], item["crop"])
        out.update(answer=reply.text, confidence=reply.confidence, stats=dict(reply.usage.stats))
    except ProviderUnavailable as e:
        out.update(status="unavailable", error=str(e)[:200])
    except ProviderError as e:
        out.update(status=classify(e), error=str(e)[:200])
    out["wall"] = time.perf_counter() - started
    return out


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def run_one(sampler: benchlib.GpuSampler, model: str, run: int, position: int, items: list[dict]) -> dict:
    unload_all()
    time.sleep(1.0)
    t_idle0 = time.time()
    time.sleep(4.0)
    idle = sampler.window(t_idle0, time.time())
    provider = ask.OllamaChoiceProvider(model, timeout=TIMEOUT, keep_alive="30m")
    requests_before = served_requests()
    t_cold0 = time.time()
    # The load: one question that is not in the set (the first one with its field name changed,
    # so the timed first question cannot be answered from Ollama's prompt cache); discarded.
    first = items[0]
    warm_item = {**first, "id": "warmup", "question": dataclasses.replace(
        first["question"], name="warm-up", tooltip="")}
    warm = put_question(provider, warm_item)
    t_cold1 = time.time()
    ps = {m["name"]: m for m in api("/api/ps").get("models", [])}.get(model, {})
    t0 = time.time()
    results = [put_question(provider, item) for item in items]
    t1 = time.time()
    # Determinism: ask a fixed seeded sample again, right after the pass. Not timed (an identical
    # prompt may be answered from Ollama's prompt cache); only the answers are compared.
    sample = sorted(random.Random(SAMPLE_SEED).sample(range(len(items)), REPEATS))
    repeats = [put_question(provider, items[i]) for i in sample]
    time.sleep(0.5)
    requests_after = served_requests()
    window = sampler.window(t0, t1)
    whole = sampler.window(t_cold0, t1)
    elapsed = t1 - t0
    return {
        "model": model, "run": run + 1, "position": position + 1, "warmup": warm,
        "cold_seconds": t_cold1 - t_cold0, "results": results, "repeats": repeats, "elapsed": elapsed,
        "idle_watts": mean([s[1] for s in idle]), "idle_mib": mean([s[2] for s in idle]),
        "avg_watts": mean([s[1] for s in window]), "avg_util": mean([s[3] for s in window]),
        "peak_mib": max((s[2] for s in whole), default=float("nan")),
        "samples": len(window), "ps_size": ps.get("size"), "ps_size_vram": ps.get("size_vram"),
        "ps_context": ps.get("context_length"),
        "server_posts": requests_after - requests_before, "client_posts": len(results) + len(repeats) + 1,
    }


def run_all(work: Path, models: list[str], runs: int, first: int = 1, rotate: bool = True) -> None:
    items = load_questions(work)["items"]
    done = set()
    results_path = work / "results.jsonl"
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            row = json.loads(line)
            done.add((row["model"], row["run"]))
    print(f"{len(items)} questions, {len(models)} models, {runs} runs; {len(done)} already done")
    sampler = benchlib.GpuSampler("power.draw,memory.used,utilization.gpu", 200)
    try:
        for r in range(first - 1, first - 1 + runs):
            shift = r % len(models) if rotate else 0
            order = models[shift:] + models[:shift]
            for position, model in enumerate(order):
                if (model, r + 1) in done:
                    continue
                row = run_one(sampler, model, r, position, items)
                with results_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                statuses = defaultdict(int)
                for res in row["results"]:
                    statuses[res["status"]] += 1
                lat = sorted(res["wall"] for res in row["results"])
                print(f"run {r + 1} #{position + 1} {model:14} cold {row['cold_seconds']:6.1f}s  "
                      f"median {lat[len(lat) // 2]:.2f}s  {dict(statuses)}  "
                      f"{row['avg_watts']:.0f}W  peak {row['peak_mib']:.0f} MiB  "
                      f"posts {row['server_posts']}/{row['client_posts']}", flush=True)
                if row["warmup"]["status"] == "unavailable":
                    print("  model unavailable: " + row["warmup"]["error"], flush=True)
    finally:
        sampler.close()
        unload_all()


# -- report ----------------------------------------------------------------------------------


def build_cache(items_by_id: dict, row_results: list[dict], model: str) -> dict:
    """The replay cache one run would have recorded, as the pipeline writes it."""
    cache = {}
    for res in row_results:
        if res["status"] != "ok" or res["answer"] is None:
            continue
        item = items_by_id[res["id"]]
        q = item["question"]
        mod, version = ((la, la.PROMPT_VERSION) if item["kind"] == "label"
                        else (sa, sa.STRUCTURE_PROMPT_VERSION))
        entry = {"kind": item["kind"], "answer": res["answer"], "model": model,
                 "prompt_version": version, "page": q.page, "node": q.node}
        if res["confidence"] is not None:
            entry["confidence"] = round(res["confidence"], 3)
        cache[mod.cache_key(item["sha"], q, model)] = entry
    return cache


def no_call(_q):
    raise AssertionError("replay only")


def score(data: dict, cache: dict, model: str) -> dict:
    """Replay `cache` over the rules-only documents: correct counts and what the checks rejected."""
    label_spec = json.loads(ela.LABELS.read_text())["forms"]
    structure_spec = json.loads(esa.LABELS.read_text())["forms"]
    out = {"label_correct": 0, "label_total": 0, "structure_correct": 0, "structure_total": 0,
           "rejected": 0, "accepted": 0, "label_rules": 0, "structure_rules": 0}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cache.json"
        path.write_text(json.dumps(cache))
        for kind, spec, mod, key in (("label", label_spec, la, "labels"),
                                     ("structure", structure_spec, sa, "lines")):
            for form, entry in spec.items():
                rules = Document.from_json(data["docs"][(kind, form)]["json"])
                doc = Document.from_json(data["docs"][(kind, form)]["json"])
                stats = mod.assist(doc, None, path, model=model, image_for=no_call)
                out["rejected"] += stats["rejected"]
                out["accepted"] += stats["accepted"]
                finder, judge = ((ela.find, ela.correct) if kind == "label"
                                 else (esa.find, esa.correct))
                for item in entry[key]:
                    a, b = finder(rules, item), finder(doc, item)
                    out[f"{kind}_total"] += 1
                    out[f"{kind}_rules"] += int(a is not None and judge(a, item))
                    out[f"{kind}_correct"] += int(b is not None and judge(b, item))
    return out


def report(work: Path, csv_prefix: Path, record_caches: bool) -> None:
    data = load_questions(work)
    items_by_id = {it["id"]: it for it in data["items"]}
    rows = [json.loads(line) for line in (work / "results.jsonl").read_text().splitlines()]
    models = [m for m in MODELS if any(r["model"] == m for r in rows)]
    per_run, per_question = [], []
    summary = {}
    for model in models:
        mrows = sorted((r for r in rows if r["model"] == model), key=lambda r: r["run"])
        answers = defaultdict(list)
        run_medians, all_lat, scores = [], [], []
        repeat_diffs, repeat_n = [], 0
        for row in mrows:
            sc = score(data, build_cache(items_by_id, row["results"], model), model)
            scores.append(sc)
            lat = [res["wall"] for res in row["results"] if res["status"] == "ok"]
            run_medians.append(statistics.median(lat) if lat else float("nan"))
            all_lat += lat
            counts = defaultdict(int)
            for res in row["results"]:
                counts[res["status"]] += 1
                answers[res["id"]].append(res["answer"])
                st = res["stats"]
                use = st.get("usage") or {}
                per_question.append({
                    "model": model, "run": row["run"], "question": res["id"],
                    "status": res["status"], "answer": res["answer"],
                    "confidence": res["confidence"], "latency_s": round(res["wall"], 4),
                    "prompt_tokens": st.get("prompt_eval_count", use.get("input_tokens")),
                    "output_tokens": st.get("eval_count", use.get("output_tokens")),
                    "prompt_eval_s": round(st["prompt_eval_duration"] / 1e9, 4)
                    if "prompt_eval_duration" in st else None,
                    "eval_s": round(st["eval_duration"] / 1e9, 4) if "eval_duration" in st else None,
                    "load_s": round(st["load_duration"] / 1e9, 4) if "load_duration" in st else None,
                    "error": res["error"]})
            first_answer = {res["id"]: res["answer"] for res in row["results"]}
            repeat_diff = [rep["id"] for rep in row.get("repeats", [])
                           if rep["answer"] != first_answer.get(rep["id"])]
            repeat_diffs.extend(repeat_diff)
            repeat_n += len(row.get("repeats", []))
            n = len(row["results"])
            net_w = row["avg_watts"] - row["idle_watts"]
            wh_gross = row["avg_watts"] * row["elapsed"] / 3600 / n
            wh_net = net_w * row["elapsed"] / 3600 / n
            per_run.append({
                "model": model, "run": row["run"], "position": row["position"],
                "cold_first_request_s": round(row["cold_seconds"], 2),
                "warmup_status": row["warmup"]["status"], "questions": n,
                "ok": counts["ok"], "timeout": counts["timeout"], "http_error": counts["http_error"],
                "malformed": counts["malformed"], "unavailable": counts["unavailable"],
                "rejected_by_checks": sc["rejected"], "accepted": sc["accepted"],
                "label_correct": sc["label_correct"], "structure_correct": sc["structure_correct"],
                "elapsed_s": round(row["elapsed"], 2), "median_latency_s": round(run_medians[-1], 3),
                "avg_watts": round(row["avg_watts"], 1), "idle_watts": round(row["idle_watts"], 1),
                "gpu_util_pct": round(row["avg_util"], 1), "power_samples": row["samples"],
                "wh_per_question_gross": round(wh_gross, 5), "wh_per_question_net": round(wh_net, 5),
                "idle_mib": round(row["idle_mib"]), "peak_mib": round(row["peak_mib"]),
                "peak_vram_above_idle_mib": round(row["peak_mib"] - row["idle_mib"]),
                "ollama_size_bytes": row["ps_size"], "ollama_size_vram_bytes": row["ps_size_vram"],
                "ollama_context": row["ps_context"], "server_posts": row["server_posts"],
                "client_posts": row["client_posts"],
                "repeat_asked": len(row.get("repeats", [])), "repeat_mismatches": len(repeat_diff)})
        pr = [p for p in per_run if p["model"] == model]
        differing = [qid for qid, a in answers.items() if len(set(a)) > 1]
        summary[model] = {
            "runs": len(mrows), "scores": scores, "run_medians": run_medians, "all_lat": all_lat,
            "differing": differing, "pr": pr, "answers": answers,
            "repeat_diffs": repeat_diffs, "repeat_n": repeat_n}
    write_csv(csv_prefix.with_name(csv_prefix.name + "-runs.csv"), per_run)
    write_csv(csv_prefix.with_name(csv_prefix.name + "-questions.csv"), per_question)
    print_summary(summary, rows, items_by_id)
    confidence_report(summary, per_question, items_by_id, data)
    anthropic_cost(data)
    if record_caches:
        record(summary, rows, items_by_id)


HAIKU_IN, HAIKU_OUT = providers.PRICES[providers.CLAUDE_HAIKU]   # $ per million tokens


def anthropic_cost(data: dict) -> None:
    """Dollars per 1,000 questions for the Claude Haiku reference, from the request sizes.

    Image tokens: width x height / 750 (Anthropic's rule of thumb for a base64 image). Text
    tokens: characters / 3.5 over the system prompt and the JSON text (an estimate; the real
    count would come from messages.count_tokens). Output: the {"candidate": ...} object, about
    15 tokens, plus an assumed allowance for thinking at effort low."""
    import io

    from PIL import Image
    n, tokens_in, image_tokens = 0, 0.0, 0.0
    for item in data["items"]:
        q = item["question"]
        w, h = Image.open(io.BytesIO(item["image"])).size
        text = len(q.system_prompt) + len(q.prompt(item["crop"])) + len(q.instruction)
        image_tokens += w * h / 750
        tokens_in += w * h / 750 + text / 3.5
        n += 1
    mean_in = tokens_in / n
    print(f"\nClaude Haiku ({n} questions): mean input about {mean_in:.0f} tokens "
          f"({image_tokens / n:.0f} of them the image)")
    for label, out in (("answer only, 15 output tokens", 15), ("with 300 thinking tokens", 315)):
        cost = 1000 * (mean_in * HAIKU_IN + out * HAIKU_OUT) / 1e6
        print(f"  {label}: ${cost:.4f} per 1,000 questions")


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def span(values: list) -> str:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return "-"
    lo, hi = min(vals), max(vals)
    return f"{lo}" if lo == hi else f"{lo}-{hi}"


def print_summary(summary: dict, rows: list[dict], items_by_id: dict) -> None:
    print("\nmodel | runs | labels/51 | structure/36 | run-median latency s (median, range) | p95 s |"
          " cold first request s (median, range) | peak VRAM above idle MiB | failed (timeout/http/malformed)"
          " | rejected | Wh/q gross, net | $/1000q gross | nondeterministic questions")
    for model, s in summary.items():
        pr = s["pr"]
        meds = s["run_medians"]
        gross = statistics.median(p["wh_per_question_gross"] for p in pr)
        net = statistics.median(p["wh_per_question_net"] for p in pr)
        cold = [p["cold_first_request_s"] for p in pr]
        print(f"{model} | {s['runs']} | {span([c['label_correct'] for c in s['scores']])} | "
              f"{span([c['structure_correct'] for c in s['scores']])} | "
              f"{statistics.median(meds):.2f} ({min(meds):.2f}-{max(meds):.2f}) | "
              f"{benchlib.percentile(s['all_lat'], 0.95):.2f} | {statistics.median(cold):.1f} "
              f"({min(cold):.1f}-{max(cold):.1f}) | "
              f"{statistics.median(p['peak_vram_above_idle_mib'] for p in pr)} | "
              f"{sum(p['timeout'] + p['http_error'] + p['malformed'] + p['unavailable'] for p in pr)}"
              f" ({sum(p['timeout'] for p in pr)}/{sum(p['http_error'] for p in pr)}/"
              f"{sum(p['malformed'] for p in pr)}) | "
              f"{span([p['rejected_by_checks'] for p in pr])} | {gross:.4f}, {net:.4f} | "
              f"{gross * KWH_PRICE:.4f} | {len(s['differing'])}")
    print("\nrules only:", {k: v for k, v in list(summary.values())[0]["scores"][0].items()
                            if k.endswith("_rules") or k.endswith("_total")})
    print("\nrequests: server POSTs vs client requests per run (must match)")
    for model, s in summary.items():
        print(f"  {model}: " + ", ".join(f"{p['server_posts']}/{p['client_posts']}" for p in s["pr"]))
    print("\nlimiter (chat models, mean over timed questions): prompt eval vs generation")
    by_model = defaultdict(lambda: defaultdict(list))
    for row in rows:
        for res in row["results"]:
            st = res["stats"]
            if "prompt_eval_duration" in st:
                by_model[row["model"]]["pe"].append(st["prompt_eval_duration"] / 1e9)
                by_model[row["model"]]["ev"].append(st.get("eval_duration", 0) / 1e9)
                by_model[row["model"]]["pt"].append(st.get("prompt_eval_count", 0))
                by_model[row["model"]]["ot"].append(st.get("eval_count", 0))
                by_model[row["model"]]["wall"].append(res["wall"])
            elif "usage" in st:
                by_model[row["model"]]["pt"].append(st["usage"].get("input_tokens", 0))
                by_model[row["model"]]["ot"].append(st["usage"].get("output_tokens", 0))
                by_model[row["model"]]["wall"].append(res["wall"])
    for model, d in by_model.items():
        pe, ev = mean(d.get("pe", [])), mean(d.get("ev", []))
        print(f"  {model}: wall {mean(d['wall']):.3f}s; prompt eval {pe:.3f}s ({mean(d['pt']):.0f} tokens);"
              f" generation {ev:.3f}s ({mean(d['ot']):.1f} tokens)")
    print("\nnondeterminism (questions whose answers differ across runs):")
    for model, s in summary.items():
        print(f"  {model}: re-asked {s['repeat_n']} sampled questions right after the pass, "
              f"{len(s['repeat_diffs'])} answered differently; across passes {len(s['differing'])} differ " + "; ".join(
            f"{qid} -> {sorted(set(map(str, s['answers'][qid])))}" for qid in s["differing"][:6]))


def confidence_report(summary: dict, per_question: list[dict], items_by_id: dict, data: dict) -> None:
    """Does a decision model's confidence separate its right answers from its wrong ones?"""
    truth = ground_truth(data, items_by_id)
    print("\nconfidence as a reject threshold (decision models, all runs pooled)")
    for model in ("clef-flash:9b", "d1:3b"):
        pts = [(p["confidence"], p["answer"] == truth.get(p["question"]))
               for p in per_question if p["model"] == model and p["confidence"] is not None
               and p["question"] in truth]
        if not pts:
            continue
        right = [c for c, ok in pts if ok]
        wrong = [c for c, ok in pts if not ok]
        print(f"  {model}: {len(pts)} answers with a truth; right {len(right)} (median conf "
              f"{statistics.median(right) if right else float('nan'):.3f}), wrong {len(wrong)} "
              f"(median conf {statistics.median(wrong) if wrong else float('nan'):.3f})")
        if right and wrong:
            wins = sum((r > w) + 0.5 * (r == w) for r in right for w in wrong)
            print(f"    AUC (chance a right answer has the higher confidence): "
                  f"{wins / (len(right) * len(wrong)):.2f}")
        for t in (0.5, 0.7, 0.8, 0.9, 0.95, 0.99):
            kept = [(c, ok) for c, ok in pts if c >= t]
            print(f"    threshold {t}: keeps {len(kept)}/{len(pts)}, "
                  f"{sum(ok for _, ok in kept)}/{len(kept)} right; rejects "
                  f"{sum(not ok for c, ok in pts if c < t)}/{len(wrong)} wrong, "
                  f"{sum(ok for c, ok in pts if c < t)}/{len(right)} right")


def ground_truth(data: dict, items_by_id: dict) -> dict:
    """The expected answer of each asked question where the hand labels settle it.

    Structure: the hand label is heading / label / value for the line (a label or value line
    is right when the model says label or value, a heading when it says heading; "unsure" is
    never right). Labels: the answer is right when the candidate's text contains the caption."""
    truth = {}
    structure = json.loads(esa.LABELS.read_text())["forms"]
    labels = json.loads(ela.LABELS.read_text())["forms"]
    for qid, item in items_by_id.items():
        q = item["question"]
        if item["kind"] == "structure":
            line = next((ln for ln in structure[item["form"]]["lines"]
                         if ln["page"] == q.page and abs(ln["at"][0] - q.bbox[0]) <= 3
                         and abs(ln["at"][1] - q.bbox[1]) <= 3), None)
            if line:
                truth[qid] = ("heading" if line["kind"] == "heading" else line["kind"])
        else:
            doc = Document.from_json(data["docs"][("label", item["form"])]["json"])
            node = next((n for n in doc.root.walk() if n.id == q.node), None)
            hit = None
            for lab in labels[item["form"]]["labels"]:
                if node is not None and ela.find(doc, lab) is node:
                    hit = lab
            if hit is None:
                continue
            wants = [ela.norm(w) for w in [hit["caption"], *hit.get("accept", [])]]
            ok = [c.id for c in q.candidates if any(w in ela.norm(c.text) for w in wants)]
            truth[qid] = ok[0] if ok else ask.NONE
    return truth


def record(summary: dict, rows: list[dict], items_by_id: dict) -> None:
    """Merge each model's answers (its first run with no failed question) into the replay caches."""
    for model in summary:
        mrows = sorted((r for r in rows if r["model"] == model), key=lambda r: r["run"])
        pick = next((r for r in mrows if all(x["status"] == "ok" for x in r["results"])), mrows[0])
        cache = build_cache(items_by_id, pick["results"], model)
        for path, kind in ((ela.CACHE, "label"), (esa.CACHE, "structure")):
            current = ask.load_cache(path)
            mine = {k: v for k, v in cache.items() if v["kind"] == kind}
            current.update(mine)
            path.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"recorded {model} from run {pick['run']}: {len(cache)} answers")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["extract", "run", "report"])
    parser.add_argument("--work", type=Path, default=Path("/tmp/bench"))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--first-run", type=int, default=1, help="the number of the first pass")
    parser.add_argument("--in-order", action="store_true", help="use the --model order as given")
    parser.add_argument("--model", action="append")
    parser.add_argument("--csv-prefix", type=Path, default=REPO / "docs/assist-bench")
    parser.add_argument("--record-caches", action="store_true")
    args = parser.parse_args()
    if args.command == "extract":
        extract(args.work)
    elif args.command == "run":
        run_all(args.work, args.model or MODELS, args.runs, args.first_run, not args.in_order)
    else:
        report(args.work, args.csv_prefix, args.record_caches)


if __name__ == "__main__":
    main()
