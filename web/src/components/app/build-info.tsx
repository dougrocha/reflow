"use client";

import type { ReactNode } from "react";
import { Cpu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverDescription, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import {
  altTextLine, answersLine, commitLabel, directCostLine, directPagesLine, dollarsLabel, engineLabel, ocrLine,
  pdfMadeLine, perThousandLabel, polishCostLine, polishLine, providerLabel, timingLine, timingsView, usedAi,
  type BuildInfo, type DirectRun, type PolishRun,
} from "@/lib/build-info";
import { plural, secondsLabel } from "@/lib/format";
import { variantOf } from "@/pipelines";

/** "2026-11-04 20:14 UTC": the same on the server and in the browser. */
const builtAt = (iso: string) => `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;

function Row({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-x-3 gap-y-0.5">
      <dt className="text-muted-foreground">{term}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

const Model = ({ model, provider }: { model: string; provider: string }) => (
  <><code className="type-mono break-all">{model}</code> <span className="text-muted-foreground">({providerLabel(provider)})</span></>
);

/** What each page of a model run cost to make. */
function PerPage({ run }: { run: DirectRun }) {
  if (!run.perPage.length) return null;
  return (
    <details className="type-caption mt-1">
      <summary className="cursor-pointer text-muted-foreground">Per page</summary>
      <table className="mt-1 w-full tabular-nums">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th scope="col" className="font-normal">Page</th>
            <th scope="col" className="font-normal">Tokens in</th>
            <th scope="col" className="font-normal">Out</th>
            <th scope="col" className="font-normal">Cost</th>
            <th scope="col" className="font-normal">Time</th>
          </tr>
        </thead>
        <tbody>
          {run.perPage.map((p) => (
            <tr key={p.page}>
              <th scope="row" className="font-normal">{p.page}{p.cached ? " (cached)" : ""}</th>
              <td>{p.tokensIn.toLocaleString("en-US")}</td>
              <td>{p.tokensOut.toLocaleString("en-US")}</td>
              <td>{dollarsLabel(p.dollars)}</td>
              <td>{secondsLabel(p.seconds)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

/** A model run's headline cost: per 1,000 pages, the figure the eval table uses too. */
const PerThousand = ({ run }: { run: DirectRun }) => (
  <div className="font-medium" data-testid="per-thousand">{perThousandLabel(run)}</div>
);

/** A direct build's model, what it read, and what it cost: per 1,000 pages first, this document's
 *  exact cost in small text. */
function DirectDetails({ run }: { run: DirectRun }) {
  return (
    <>
      <Model model={run.model} provider={run.provider} />
      <PerThousand run={run} />
      <div className="type-caption text-muted-foreground">{directPagesLine(run)}</div>
      <div className="type-caption text-muted-foreground" data-testid="direct-cost">{directCostLine(run)}</div>
      <PerPage run={run} />
    </>
  );
}

/** A polish run's model, its edits, and what it cost. */
function PolishDetails({ run }: { run: PolishRun }) {
  return (
    <>
      <Model model={run.model} provider={run.provider} />
      <PerThousand run={run} />
      <div className="type-caption text-muted-foreground">{polishLine(run)}</div>
      <div className="type-caption text-muted-foreground" data-testid="polish-cost">{polishCostLine(run)}</div>
      <PerPage run={run} />
    </>
  );
}

/** Where the build's time went: the total, then each stage, largest first. Nothing for a build that
 *  recorded no timings. */
function Timings({ timings }: { timings: BuildInfo["timings"] }) {
  const view = timingsView(timings);
  if (!view) return null;
  return (
    <Row term="Time">
      <div className="font-medium tabular-nums" data-testid="build-total">{secondsLabel(view.total)} in all</div>
      {view.rows.length ? (
        <ul aria-label="Time per stage" className="type-caption text-muted-foreground tabular-nums" data-testid="build-stages">
          {view.rows.map((r) => <li key={r.stage}>{timingLine(r)}</li>)}
        </ul>
      ) : null}
    </Row>
  );
}

const OLD_BUILD = "This document was built before builds recorded how they were made. Build it again to see the engine, OCR and models.";

/** The panel's content: what made the transcript, from its build record. Outside a popover too
 *  (the compare screen's column headers). */
export function BuildInfoDetails({ build, slug }: { build: BuildInfo | null; slug: string }) {
  const variant = variantOf(slug)?.pipeline.label;
  if (!build) return <p className="text-sm">{OLD_BUILD}</p>;
  const made = pdfMadeLine(build.pdf);
  return (
    <dl className="space-y-2 text-sm" data-testid="build-info">
      {variant ? <Row term="Variant">{variant}</Row> : null}
      {build.pdf ? <Row term="PDF made by"><span data-testid="pdf-made">{made ?? "Not named in the PDF"}</span></Row> : null}
      <Row term="Layout">{engineLabel(build.engine)}</Row>
      <Row term="OCR">{build.engine === "direct" ? "None: the model reads each page's picture" : ocrLine(build.ocr)}</Row>
      {build.direct ? <Row term="Transcription"><DirectDetails run={build.direct} /></Row> : null}
      {build.polish ? <Row term="Polish"><PolishDetails run={build.polish} /></Row> : null}
      <Row term="AI">
        {usedAi(build) ? (
          build.ai.length ? (
            <ul className="space-y-1.5">
              {build.ai.map((s) => (
                <li key={s.step}>
                  <span className="font-medium">{s.step}:</span> <Model model={s.model} provider={s.provider} />
                  {answersLine(s) ? <div className="type-caption text-muted-foreground">{answersLine(s)}</div> : null}
                </li>
              ))}
            </ul>
          ) : build.direct ? "The transcription above" : build.polish ? "The polish above" : "Only for alt text"
        ) : "None. No AI model took part in this transcript."}
      </Row>
      <Row term="Alt text">
        {build.altText ? (
          <>
            <Model model={build.altText.model} provider={build.altText.provider} />
            <div className="type-caption text-muted-foreground">{altTextLine(build.altText)}</div>
          </>
        ) : "None drafted: figures without alt text go to review"}
      </Row>
      <Row term="Review">
        {build.decisionsApplied ? `${plural(build.decisionsApplied, "reviewer decision")} applied` : "No reviewer decisions applied"}
      </Row>
      <Timings timings={build.timings} />
      <Row term="Converter">
        {build.converter ? <code className="type-mono">{commitLabel(build.converter)}</code> : "Unknown commit"}
        <div className="type-caption text-muted-foreground">Built {builtAt(build.built)}</div>
      </Row>
    </dl>
  );
}

/** The header button that opens "How this transcript was made", in the viewer and the inspector. */
export function BuildInfoButton({ build, slug }: { build: BuildInfo | null; slug: string }) {
  return (
    <Popover>
      <PopoverTrigger render={<Button variant="outline" size="lg" aria-label="How this transcript was made" />}>
        <Cpu aria-hidden="true" />
        <span className="max-lg:hidden">How it was made</span>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(26rem,calc(100vw-1rem))] space-y-3">
        <PopoverTitle>How this transcript was made</PopoverTitle>
        {build ? <BuildInfoDetails build={build} slug={slug} /> : <PopoverDescription>{OLD_BUILD}</PopoverDescription>}
      </PopoverContent>
    </Popover>
  );
}
