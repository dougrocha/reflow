import type { DocumentSummary } from "@/lib/documents";

export function DocumentFacts({ doc }: { doc: DocumentSummary }) {
  const converted = doc.pages !== null;
  const rows: [string, React.ReactNode][] = [
    ["Pages", converted ? doc.pages!.toLocaleString("en-US") : "—"],
    ["Status", converted ? "Converted" : "Not converted"],
    ...(converted ? ([["Errors", doc.errors], ["Warnings", doc.warnings]] as [string, React.ReactNode][]) : []),
    ["Original", <a key="pdf" href={doc.pdfHref} download>Download PDF</a>],
    ...(converted
      ? ([[
          "Transcript",
          <a key="t" href={`${doc.base}index.html`} target="_blank" rel="noopener">
            Printable transcript<span className="sr-only"> (opens in a new tab)</span>
          </a>,
        ]] as [string, React.ReactNode][])
      : []),
  ];
  return (
    <aside aria-labelledby="facts-heading" className="self-start rounded-xl p-5 ring-1 ring-foreground/10 lg:sticky lg:top-20">
      <h2 id="facts-heading" className="type-h3 mb-4">About this document</h2>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 type-small">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="text-right tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}
