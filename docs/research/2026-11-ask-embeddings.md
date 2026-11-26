# Qwen3 embeddings for "Ask a question" retrieval

Measured 2026-11-01 on an RTX 3090 (24 GB), with Ollama and the golden set
(`web/scripts/ask-golden.ts`, 107 answerable questions on 12 documents). Everything ran locally.

## Recommendation

**Keep BM25 as the default. Don't turn on embeddings.** No variant met the success
criteria:

- Every variant that ranks by embeddings loses passages from the top 8 that BM25 finds. The best,
  hybrid with 4b, has 102 against BM25's 103.
- Hybrid raises recall@1 by 3 or 4 questions (72 to 73 against 69). The gain is on `hold` and
  `new`, not on `confirm`, and 3 or 4 questions out of 107 is within the noise the plan warned about.
- No hybrid recovers 2 of the current misses. Embeddings alone recover 2, but drop 6 to 11
  questions that BM25 had.
- 0.6b fits next to `gemma4:e4b` (1.0 GB of video memory with a 2K context), and a question
  takes 8 ms on the golden documents. On maricopa-directory, though, it takes 99 ms, and embedding
  that document takes 11 minutes, which is far more than its build.

The retriever stays in the code as an opt-in experiment (`REFLOW_ASK_RETRIEVER=embed|hybrid`).
Next time, a better way to fuse the two rankings is the lever to try, not a bigger model (see
[What to try next](#what-to-try-next)).

## Baseline: BM25 today

`bun run ask:eval` on documents rebuilt from current code gives **recall@1 69/107 and recall@8
103/107**. The plan quoted 68 and 102 from 2026-10-28. The converter changed in between, and
"What phone number reaches the Social Security Administration?" is now at rank 5. The documents
built earlier were stale: 31 of the 94 predated heading passages. On those, the baseline came out as 67 and 100.

## Variants

`web/src/lib/ask/retriever.ts`. Ollama `POST /api/embed` with `num_ctx` 2048. The document side is
each passage's trail, lead-in and text, cut to 2,000 characters. The query side uses the model
card's one-line instruction. The vectors are normalised and compared by cosine.

- **embed**: the top 8 by cosine, no score floor.
- **hybrid**: reciprocal rank fusion (k = 60) of the BM25 ranking and the embedding ranking, each
  cut to 100.

## Recall

Each set's cell is recall@1 / recall@8.

| Variant | recall@1 | recall@8 | tune (28) | hold (12) | new (47) | confirm (20) |
| --- | --- | --- | --- | --- | --- | --- |
| **BM25 (default)** | **69** | **103** | 18 / 27 | 9 / 12 | 27 / 44 | 15 / 20 |
| embed 0.6b | 69 | 94 | 18 / 23 | 10 / 11 | 30 / 42 | 11 / 18 |
| hybrid 0.6b | 73 | 100 | 16 / 25 | 11 / 12 | 32 / 44 | 14 / 19 |
| embed 4b | 68 | 99 | 14 / 27 | 11 / 12 | 29 / 43 | 14 / 17 |
| hybrid 4b | 72 | 102 | 18 / 27 | 11 / 12 | 29 / 44 | 14 / 19 |
| embed 8b | 69 | 96 | 15 / 26 | 9 / 11 | 33 / 42 | 12 / 17 |
| hybrid 8b | 73 | 101 | 19 / 26 | 10 / 12 | 31 / 44 | 13 / 19 |

On `confirm`, every embedding variant is at or below BM25 at both 1 and 8. On `new`, hybrid gains
2 to 5 questions at 1 and none at 8.

### The issue's 5 misses

This table gives the rank of the expected passage, or "miss" when it isn't in the top 8.

| Question | BM25 | embed 0.6b | hybrid 0.6b | embed 4b | hybrid 4b | embed 8b | hybrid 8b |
| --- | --- | --- | --- | --- | --- | --- | --- |
| What is the formula of a residual building block? | miss | miss | miss | 4 | miss | 4 | miss |
| At what income do social security benefits become taxable for a single person? | miss | miss | miss | miss | miss | miss | miss |
| How long can a deposition last? | miss | 1 | miss | 1 | miss | 1 | miss |
| What phone number reaches the Social Security Administration? | 5 | 1 | 2 | 1 | 2 | 1 | 3 |
| What is verification? | miss | 4 | 8 | miss | miss | miss | miss |

Every size ranks the deposition passage first by itself, yet no hybrid keeps it. BM25 shares no
word with that passage, so the passage gets a single 1/61 share from fusion. Passages that both
rankings place lower add up to more. That is the main weakness of this fusion for the problem
here.

### Questions BM25 had in the top 8 that each variant drops

- **embed 0.6b (11).** resnet: top-5 ensemble error, the competition won, COCO improvement.
  frcp-2025: days to answer after service, motion to dismiss as summary judgment, discovery
  materials not filed, matter admitted. scotus: what the Board should be ordered to do, the
  storybooks' school year. nasa: product validation. epa: carbon sequestration offset.
- **hybrid 0.6b (4).** frcp-2025: days to answer after service, discovery materials not filed.
  epa: carbon sequestration offset. scotus: the storybooks' school year.
- **embed 4b (6).** frcp-2025: days to answer. resnet: parameters of the 1202-layer net. nasa:
  product validation. scotus: the storybooks' school year. irs-w4: the Multiple Jobs Worksheet.
  uscis-i9: the supplement for reverification.
- **hybrid 4b (1).** scotus: the storybooks' school year.
- **embed 8b (9).** resnet: top-5 ensemble error. frcp-2025: days to answer, summons contents.
  fw9: the exempt payee code for the United States. nasa: product validation. epa: carbon
  sequestration offset. p17: the maximum additional child tax credit. scotus: the storybooks'
  school year. census: the 2019 median income.
- **hybrid 8b (2).** resnet: top-5 ensemble error. scotus: the storybooks' school year.

### After the fact: BM25's top 7 plus the embedding's top 1

This variant takes BM25's top 7 and adds the best embedding passage that BM25 didn't return. It
was chosen after seeing every set, so it is a lead for the next experiment, not a result.

| Variant | recall@1 | recall@8 | Misses recovered |
| --- | --- | --- | --- |
| BM25 top 7 + 0.6b top 1 | 69 | 103 | deposition |
| BM25 top 7 + 4b top 1 | 69 | 102 | deposition |
| BM25 top 7 + 8b top 1 | 69 | 103 | deposition |
| BM25 top 6 + 0.6b top 2 | 69 | 101 | deposition |

It keeps BM25's recall and its rank 1 by construction, and recovers 1 miss, not 2.

## Cost

| | 0.6b | 4b | 8b |
| --- | --- | --- | --- |
| Video memory in Ollama (`ollama ps`, `num_ctx` 2048) | 985 MB | 3.1 GB | 5.4 GB |
| Video memory at Ollama's default 32K context | 4.6 GB | not measured | not measured |
| Embedding speed on the golden documents (28,437 passages) | 116 /s, 244 s | 52 /s, 547 s | 34 /s, 841 s |
| A typical document, frcp-2025 (142 pages, 2,484 passages; `docs:build` takes 43 s) | 19 s | 40 s | 62 s |
| maricopa-directory (94,257 passages) | 677 s (11.3 min), 139 /s | 1,422 s (23.7 min), 66 /s | about 41 min, estimated from a sample (39 /s) |
| Cache on disk, golden documents (text: about 12 MB of `passages.jsonl`) | 111 MB | 278 MB | 444 MB |
| Cache on disk, maricopa-directory (text: 22.3 MB) | 368 MB | 921 MB | 1.47 GB (computed) |
| A question, golden documents, warm (median) | 8.4 ms | 15.4 ms | 21.9 ms |
| A question, maricopa-directory, warm (median) | 99 ms | 238 ms | not measured |
| Bench process memory with maricopa-directory's vectors | 1.04 GB | 2.14 GB | not measured |

Notes:

- **Context.** Ollama loads qwen3-embedding with a 32K context unless told otherwise, which held
  4.6 GB for the 0.6b. Passages are at most 2,000 characters, so a 2K context returns the same
  vectors (checked: same values, same speed) with 1.0 GB. The retriever always sends `num_ctx`
  2048.
- **Speed.** Ollama embeds one input at a time. Batches of 64, 128 and 256, and 1, 2 or 4
  concurrent requests, all gave 92 to 94 passages/s on the same text with the 0.6b.
- **Question time.** On maricopa-directory, almost all of a question's time is the brute-force
  cosine over 94k vectors in JavaScript. Embedding the question itself takes about 5 to 15 ms.
- **8b on maricopa-directory.** The full run was stopped to save time. `--bench --sample
  2000` embeds every 47th passage and extrapolates. Checked against the full runs, it estimated
  12.5 min for 0.6b (measured 11.3) and 25.7 min for 4b (measured 23.7), in 20 and 37 s. For 8b it
  estimates 40.7 min.
- **GPU during the timed runs.** Each run started with `nvidia-smi` and `ollama ps`. The only
  other GPU users were desktop apps, 1.4 to 2.1 GB in all. No other compute process ran. The Ollama server was
  already running; the only models loaded on it were the ones this benchmark loaded, and each was
  stopped afterwards.
- **Fit with `gemma4:e4b`.** Every size fits next to it on 24 GB. See the next section.

## The answer side (`ask:eval -- --model`, `gemma4:e4b`)

Both runs were back to back on 2026-11-01, through the same `ask()` that the route calls.

| Retriever | In the top 8 | Found | Validated | Right | Mis-cited | Unverified | Declined | Refused of 5 unanswerable | Median per question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BM25 | 103 | 106 | 100 | 95 | 5 | 6 | 1 | 5 | 1.1 s |
| hybrid 4b | 102 | 107 | 101 | 93 | 8 | 6 | 0 | 5 | 1.0 s |

Hybrid answered one more question but cited the wrong passage three more times. That is the risk
the issue named: semantic neighbours that the model quotes well but that don't hold the answer.
Neither invented an answer to an unanswerable question. One run is a sample, and replies vary
between runs.

`gemma4:e4b` and `qwen3-embedding:4b` stayed loaded together, both 100% on the GPU. The GPU's
peak use was 10.8 GB, including about 1.6 GB for the desktop. The 0.6b would take about 2 GB less.

## What to try next

- **Fusion that keeps a lone semantic hit.** Weight the lists, or reserve a slot for the best
  embedding passage when BM25 has none of its words (the after-the-fact variant above). Write new
  questions first, because the current sets have all now been seen.
- **Search speed on large documents.** Use Matryoshka truncation to 256 or 512 dimensions (Qwen3
  supports it), int8 vectors, or an approximate index. Each would cut the 99 ms and the 368 MB, and
  each needs its recall measured.
- **Embedding throughput.** Try `OLLAMA_NUM_PARALLEL` on a private `ollama serve`, or a runtime
  that batches, such as llama.cpp's server with `--embeddings` or text-embeddings-inference. Ollama
  gave the same throughput at every batch size.
- **Build-time embedding.** If embeddings are ever on by default, embed during `docs:build`
  rather than on the first question, and only on documents small enough to fit the build's time.

## How to reproduce

```sh
cd web
bun run docs:build resnet fw9 p17 frcp-2025 scotus-mahmoud-v-taylor irs-w4-2026 uscis-i9 \
  cdc-firstline-slides-es cdc-antibiotics-trifold census-p60-273 nasa-se-handbook \
  epa-ghg-inventory-2024 maricopa-directory
ollama pull qwen3-embedding:0.6b   # and :4b, :8b
bun run ask:eval -- --json bm25.json
bun run ask:eval -- --retriever embed  --embed-model qwen3-embedding:0.6b --json embed-0.6b.json
bun run ask:eval -- --retriever hybrid --embed-model qwen3-embedding:0.6b --json hybrid-0.6b.json
bun run ask:eval -- --bench maricopa-directory --retriever hybrid --embed-model qwen3-embedding:0.6b
bun run ask:eval -- --bench maricopa-directory --retriever hybrid --sample 2000   # about a minute
```

The vectors are cached in `~/.cache/reflow/embeddings`, keyed by the conversion's version, so a
second run, or a run from another worktree with the same conversions, takes seconds.
