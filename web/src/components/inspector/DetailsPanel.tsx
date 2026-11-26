"use client";

import { useId, type ReactNode, type Ref } from "react";
import { ChevronsDownUp, ChevronsUpDown, Info } from "lucide-react";
import { SeverityBadge } from "@/components/app/severity-badge";
import { Button } from "@/components/ui/button";
import { styleFor, type InspectNode } from "@/lib/inspect";
import type { ReviewItem } from "@/lib/review";
import { DecidedNode, DecisionPanel, type Redecide } from "./DecisionPanel";

/** What the decision controls need to know about the page. */
export type Decide = { slug: string; rev: string | null; supersedes: string | null; redecide: Redecide[] };
import { cn } from "@/lib/utils";
import { isRecord } from "@/lib/guards";

// An attribute's label: "table_rows" → "Table rows".
function label(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function num(n: number): string {
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(2)));
}

// One attribute value, formatted by its type. Nested objects become a nested list, arrays of
// text become bullets, numbers are rounded, and nothing is shown as raw JSON.
function Value({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  if (value === null || value === undefined) return <span className="text-muted-foreground">—</span>;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return num(value);
  if (typeof value === "string") return <span className="whitespace-pre-wrap break-words">{value}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-muted-foreground">none</span>;
    if (value.every((v) => typeof v !== "object" || v === null)) {
      return (
        <ul className="list-disc space-y-0.5 pl-5">
          {value.map((v, i) => (
            <li key={i}><Value value={v} depth={depth + 1} /></li>
          ))}
        </ul>
      );
    }
    return (
      <ol className="space-y-2">
        {value.map((v, i) => (
          <li key={i} className="border-l-2 pl-3">
            <Value value={v} depth={depth + 1} />
          </li>
        ))}
      </ol>
    );
  }
  if (isRecord(value)) {
    if (depth > 2) return <span className="text-muted-foreground">…</span>;
    return <Fields fields={value} depth={depth + 1} />;
  }
  return String(value);
}

function Fields({ fields, depth = 0 }: { fields: Record<string, unknown>; depth?: number }) {
  return (
    <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
      {Object.entries(fields).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{label(key)}</dt>
          <dd className="min-w-0">
            <Value value={value} depth={depth} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

// The selected box's details. In the side column it sits above the transcript, and its header row
// is the one row that stays when the body is collapsed. On phones it is a tab, never collapsible.
export function DetailsPanel({
  node,
  item,
  decide,
  issue,
  issueSeverity,
  suggestions,
  collapsible,
  collapsed,
  onToggleCollapsed,
  contentRef,
}: {
  node: InspectNode | null;
  /** The review item the selection is. With no node (an orphaned decision) its words and box are all there is. */
  item: ReviewItem | null;
  /** Where the decision controls get their build; null draws none. */
  decide: Decide | null;
  /** The review item this node is, as "Error: Figure missing alt text", or null. */
  issue: string | null;
  issueSeverity: "error" | "warning" | null;
  /** The item's OCR suggestions, as "OCR read ‘8011O’ · suggested ‘80110’ (ZIP code digit)". */
  suggestions: { text: string }[];
  /** In the side column: a header-row collapse toggle, and the empty state is the one-line hint. */
  collapsible: boolean;
  collapsed: boolean;
  onToggleCollapsed?: () => void;
  contentRef?: Ref<HTMLDivElement>;
}) {
  const bodyId = useId();
  const headingId = useId();
  const style = node ? styleFor(node.role) : null;

  return (
    <section aria-labelledby={headingId} className="flex h-full min-h-0 flex-col bg-card">
      <header className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        {node && style ? (
          <>
            <span aria-hidden="true" className="inline-block size-3 shrink-0 rounded-sm ring-1 ring-foreground/25" style={{ backgroundColor: style.color }} />
            <h2 id={headingId} className="max-w-[50%] shrink-0 truncate type-label font-semibold">{style.label}</h2>
            <span className="min-w-0 shrink truncate type-small text-muted-foreground">
              · {Math.round(node.confidence * 100)}% · {node.source}
            </span>
            {issueSeverity ? <SeverityBadge severity={issueSeverity} className="shrink-0" /> : null}
          </>
        ) : item ? (
          <>
            <h2 id={headingId} className="max-w-[60%] shrink-0 truncate type-label font-semibold">{item.label ?? "Review issue"}</h2>
            <span className="min-w-0 shrink truncate type-small text-muted-foreground">· no block, only a box on the page</span>
            {issueSeverity ? <SeverityBadge severity={issueSeverity} className="shrink-0" /> : null}
          </>
        ) : collapsible ? (
          <>
            <h2 id={headingId} className="sr-only">Details</h2>
            <p className="flex min-w-0 items-center gap-2 type-small text-muted-foreground">
              <Info aria-hidden="true" className="size-4 shrink-0" />
              <span className="truncate">Select a box on the page or a block in the transcript.</span>
            </p>
          </>
        ) : (
          <h2 id={headingId} className="type-label font-semibold">Details</h2>
        )}
        {collapsible && (node || item) ? (
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto shrink-0"
            aria-expanded={!collapsed}
            aria-controls={bodyId}
            aria-label={collapsed ? "Expand details" : "Collapse details"}
            onClick={onToggleCollapsed}
          >
            {collapsed ? <ChevronsUpDown aria-hidden="true" /> : <ChevronsDownUp aria-hidden="true" />}
          </Button>
        ) : null}
      </header>

      {node ? (
        <div id={bodyId} hidden={collapsed} className="min-h-0 flex-1 overflow-y-auto">
          <NodeBody node={node} item={item} decide={decide} issue={issue} issueSeverity={issueSeverity} suggestions={suggestions} contentRef={contentRef} />
        </div>
      ) : item ? (
        <div id={bodyId} hidden={collapsed} className="min-h-0 flex-1 overflow-y-auto">
          <div ref={contentRef} className="space-y-4 p-4">
            <IssueBox issue={issue} issueSeverity={issueSeverity} suggestions={suggestions} />
            {decide ? <DecisionPanel slug={decide.slug} rev={decide.rev} item={item} nodeId={null} labels={{ [item.warn ?? ""]: item.label ?? "" }}
              supersedes={decide.supersedes} redecide={decide.redecide} /> : null}
          </div>
        </div>
      ) : !collapsible ? (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-muted-foreground">
          Select a box on the page or a block in the transcript.
        </div>
      ) : null}
    </section>
  );
}

function IssueBox({ issue, issueSeverity, suggestions }: {
  issue: string | null;
  issueSeverity: "error" | "warning" | null;
  suggestions: { text: string }[];
}) {
  if (!issue) return null;
  return (
    <div
      className={cn(
        "rounded-lg border p-3 type-small",
        issueSeverity === "error"
          ? "border-severity-error/40 bg-severity-error-subtle"
          : "border-severity-warning/40 bg-severity-warning-subtle",
      )}
    >
      <p className="font-medium">Review issue</p>
      <p>{issue}</p>
      {suggestions.map((s, i) => (
        <p key={`${i}-${s.text}`}>{s.text}</p>
      ))}
    </div>
  );
}

function NodeBody({
  node,
  item,
  decide,
  issue,
  issueSeverity,
  suggestions,
  contentRef,
}: {
  node: InspectNode;
  item: ReviewItem | null;
  decide: Decide | null;
  issue: string | null;
  issueSeverity: "error" | "warning" | null;
  suggestions: { text: string }[];
  contentRef?: Ref<HTMLDivElement>;
}) {
  const why = Array.isArray(node.attrs.why) ? (node.attrs.why as unknown[]).map(String) : null;
  const rest = Object.fromEntries(Object.entries(node.attrs).filter(([k]) => k !== "why"));
  const [x0, top, x1, bottom] = node.bbox.map((v) => Math.round(v));

  return (
    <div ref={contentRef} className="space-y-4 p-4">
      <IssueBox issue={issue} issueSeverity={issueSeverity} suggestions={suggestions} />

      {decide && item ? (
        <DecisionPanel slug={decide.slug} rev={decide.rev} item={item} nodeId={node.id} labels={Object.fromEntries(node.facts.map((f) => [f.code, f.label]))}
          supersedes={decide.supersedes} redecide={decide.redecide} />
      ) : null}
      {decide ? <DecidedNode slug={decide.slug} facts={node.facts} /> : null}

      {node.text ? (
        <p className="whitespace-pre-wrap break-words leading-relaxed">{node.text}</p>
      ) : null}

      {why && why.length > 0 ? (
        <section aria-labelledby="why-heading">
          <h3 id="why-heading" className="mb-2 type-label font-semibold">Why</h3>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            {why.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {node.facts && node.facts.length > 0 ? (
        <section aria-labelledby="facts-heading">
          <h3 id="facts-heading" className="mb-2 type-label font-semibold">Facts</h3>
          <ul className="space-y-1.5 text-sm">
            {node.facts.map((fact, i) => (
              <li key={`${fact.code}-${i}`}>
                <span className="font-medium">{fact.label}</span>
                <span className="text-muted-foreground"> · {fact.source}</span>
                {fact.detail ? <span className="block whitespace-pre-wrap break-words text-muted-foreground">{fact.detail}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {Object.keys(rest).length > 0 ? (
        <section aria-label="Attributes">
          <Fields fields={rest} />
        </section>
      ) : null}

      <p className="type-caption text-muted-foreground tabular-nums">
        Box (points, from top-left): {x0}, {top}, {x1}, {bottom}
      </p>
    </div>
  );
}
