import Link from "next/link";
import { PageHeader } from "@/components/app/page-header";
import { Kbd } from "@/components/ui/kbd";
import { VIEWER_SHORTCUTS, shortcutKeys } from "@/reflow/react/shortcuts";

export const metadata = { title: "About" };

const SHORTCUTS: { keys: string[]; action: string; where: string }[] = [
  // The transcript viewer's own list, also shown by its Keyboard shortcuts button.
  ...VIEWER_SHORTCUTS.map((s) => ({ keys: shortcutKeys(s), action: s.action, where: "Transcript" })),
  { keys: ["j"], action: "Next issue", where: "Inspector" },
  { keys: ["k"], action: "Previous issue", where: "Inspector" },
  { keys: ["]"], action: "Next page", where: "Inspector" },
  { keys: ["["], action: "Previous page", where: "Inspector" },
  { keys: ["+"], action: "Zoom the page in", where: "Inspector" },
  { keys: ["−"], action: "Zoom the page out", where: "Inspector" },
  { keys: ["Ctrl", "wheel"], action: "Zoom at the pointer", where: "Inspector" },
  { keys: ["Enter"], action: "Go to the page typed in the page field", where: "Transcript, Inspector" },
  { keys: ["Esc"], action: "Close a dialog, menu or panel", where: "Everywhere" },
];

// Production estimate. MEASURED comes from `bun run docs:build` of every sample (6,446 pages): 48 CPU-minutes,
// 1.31 GB written to the data root, and gzip sizes of each document's index, first chunk and viewer assets.
// Prices are October 2026 list prices: Cloudflare Containers ($0.00002 per vCPU-second, $0.0000025 per
// GiB-second, on the $5 Workers Paid plan) and R2 ($0.015/GB-month, no egress fee, $0.36 per million reads
// after 10 million); AWS Fargate ($0.04048 per vCPU-hour, $0.004445 per GB-hour), S3 and CloudFront ($0.085/GB
// after 1 TB a month free, $0.01 per 10,000 requests after 10 million). A cloud vCPU is taken as 1.5 times
// slower than the measuring desktop's. AI assist was measured with Qwen3-VL-8B on an RTX 3090 (6.5 s a figure,
// 8 to 56 s a page); GPU rows assume a rented 24 GB GPU at $0.40 to $1 an hour, since Cloudflare has no GPUs.
const PAGES = 150_000;
const MONTHLY_PAGES = 1_000;
const MEASURED = { pages: 6_446, cpuSecondsPerPage: 0.45, kbPerPage: 200 };

type EstimateRow = { item: string; detail: string; cloudflare: string; aws: string };

const ONE_TIME: EstimateRow[] = [
  { item: "Born-digital pages", detail: "About 28 vCPU-hours of conversion, billed only while it runs", cloudflare: "≈ $3", aws: "≈ $1.50" },
  { item: "If every page is a scan", detail: "OCR adds 3 to 6 CPU-seconds a page: 190 to 375 vCPU-hours in all", cloudflare: "≈ $20 to $40", aws: "≈ $10 to $20" },
  { item: "Writing the output", detail: "About 470,000 files, written once", cloudflare: "$0", aws: "≈ $2.50" },
  { item: "AI assist: image descriptions", detail: "Optional. About 30,000 figures at 6.5 s each on a 24 GB GPU (measured): about 55 GPU-hours, or roughly a fifth with a batching server", cloudflare: "rented GPU: ≈ $10 to $55", aws: "≈ $10 to $55" },
  { item: "AI assist: hard and handwritten pages", detail: "Optional. If 10% of pages need it: 15,000 pages at 8 to 56 s each, typically 15 to 18 s (measured): about 75 GPU-hours", cloudflare: "rented GPU: ≈ $15 to $75", aws: "≈ $15 to $75" },
];

const MONTHLY: EstimateRow[] = [
  { item: "Platform", detail: "Cloudflare Containers need the Workers Paid plan; AWS has no base fee", cloudflare: "$5", aws: "$0" },
  { item: `${MONTHLY_PAGES.toLocaleString("en-US")} new or rescanned pages`, detail: "Converted when they arrive; the converter is idle the rest of the month", cloudflare: "under $0.50", aws: "under $0.50" },
  { item: "AI assist on new pages", detail: "Optional. About 200 figures and 100 hard pages a month: about one GPU-hour", cloudflare: "rented GPU: ≈ $1", aws: "≈ $1" },
  { item: "Storage", detail: "About 33 GB of transcripts, page images, review data and source PDFs, growing by about 0.2 GB a month", cloudflare: "≈ $0.50", aws: "≈ $0.75" },
  { item: "Serving to readers", detail: "Egress and requests when people open documents; see the next table", cloudflare: "$0 to ≈ $15", aws: "$0 to ≈ $170" },
];

// A click opens the transcript (35 to 100 KB compressed), loads a few more chunks as the reader scrolls and
// perhaps one page image (80 KB): about 0.25 MB and 5 requests.
const TRAFFIC: EstimateRow[] = [
  { item: "10,000 clicks", detail: "2.5 GB", cloudflare: "$0", aws: "$0" },
  { item: "100,000 clicks", detail: "25 GB", cloudflare: "$0", aws: "$0" },
  { item: "1 million clicks", detail: "250 GB", cloudflare: "$0", aws: "$0" },
  { item: "10 million clicks", detail: "2.5 TB", cloudflare: "≈ $15 in reads, less with caching", aws: "≈ $170: $130 egress, $40 requests" },
];

function EstimateTable({ caption, detail, rows }: { caption: string; detail: string; rows: EstimateRow[] }) {
  return (
    <div className="overflow-x-auto rounded-xl ring-1 ring-foreground/10">
      <table className="w-full text-left type-small">
        <caption className="px-4 py-3 text-left type-label">{caption}</caption>
        <thead className="bg-muted/50">
          <tr>
            <th scope="col" className="h-11 px-4 type-label">Item</th>
            <th scope="col" className="h-11 px-4 type-label">{detail}</th>
            <th scope="col" className="h-11 px-4 type-label">Cloudflare</th>
            <th scope="col" className="h-11 px-4 type-label">AWS</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.item} className="border-t">
              <th scope="row" className="px-4 py-3 font-semibold">{r.item}</th>
              <td className="px-4 py-3 text-muted-foreground">{r.detail}</td>
              <td className="px-4 py-3">{r.cloudflare}</td>
              <td className="px-4 py-3">{r.aws}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AboutPage() {
  return (
    <>
      <PageHeader
        title="About reflow"
        lead="reflow turns PDF documents (government forms, directories, budgets, research papers) into accessible HTML transcripts that work with screen readers, zoom and small screens."
      />
      <div className="flex max-w-3xl flex-col gap-12">
        <section aria-labelledby="what" className="flex flex-col gap-4">
          <h2 id="what">What reflow does</h2>
          <p>reflow reads the layout of every page: its reading order, headings, lists, tables and figures. It writes a transcript of each page in plain, structured HTML, and keeps an image of the original page beside it.</p>
          <p>Where reflow cannot finish a part on its own, for example a figure without alt text, a table without headers or structure it is unsure of, it lists the part for review. Each document&rsquo;s page lists its review items; the inspector shows them on the page.</p>
        </section>

        <section aria-labelledby="using" className="flex flex-col gap-4">
          <h2 id="using">Using this site</h2>
          <dl className="grid gap-4 sm:grid-cols-[10rem_1fr] sm:gap-x-6">
            <dt className="font-semibold">Documents</dt>
            <dd className="text-muted-foreground">The library. Select a title to read its transcript, or open a document&rsquo;s overview.</dd>
            <dt className="font-semibold">Review</dt>
            <dd className="text-muted-foreground">Every document with items to check, and how many.</dd>
            <dt className="font-semibold">Transcript</dt>
            <dd className="text-muted-foreground">The whole document as accessible text, with a switch to the original page images.</dd>
            <dt className="font-semibold">Inspector</dt>
            <dd className="text-muted-foreground">One page at a time: the page with the boxes reflow found, the transcript, and why each part was read as it was.</dd>
          </dl>
        </section>

        <section aria-labelledby="embed-demo" className="flex flex-col gap-4">
          <h2 id="embed-demo">Open PDF links in place</h2>
          <p>A site can open its PDF links as accessible transcripts in a dialog, so readers do not leave the page. <Link href="/demo" className="underline underline-offset-4">Try it on the demo page.</Link></p>
        </section>

        <section id="production" aria-labelledby="prod" className="flex flex-col gap-4">
          <h2 id="prod">Running reflow in production</h2>
          <p>
            An estimate for converting {PAGES.toLocaleString("en-US")} PDF pages once, then {MONTHLY_PAGES.toLocaleString("en-US")} new or
            rescanned pages a month, and serving the transcripts to readers who click a PDF link. Each PDF is converted once and the
            result is cached in object storage; the converter runs as its own service and is idle between batches. Hosting the website
            itself is not included.
          </p>
          <p className="text-muted-foreground">
            Measured in October 2026 by converting this site&rsquo;s {MEASURED.pages.toLocaleString("en-US")} sample pages on a 16-thread
            desktop CPU: {MEASURED.cpuSecondsPerPage} CPU-seconds and about {MEASURED.kbPerPage} KB of output a page. Costs use list prices
            from the same month, before tax: Cloudflare Containers and R2, and AWS Fargate, S3 and CloudFront.
          </p>
          <EstimateTable caption={`One-time: converting ${PAGES.toLocaleString("en-US")} pages`} detail="What it covers" rows={ONE_TIME} />
          <EstimateTable caption="Every month" detail="What it covers" rows={MONTHLY} />
          <EstimateTable caption="Serving to readers, by clicks a month" detail="Data sent" rows={TRAFFIC} />
          <p>
            <strong>With AI assist:</strong> describing images and reading handwritten or hard pages with a local vision model
            (Qwen3-VL-8B) adds about $25 to $130 once and about $1 a month. It runs only where the layout rules cannot finish, and
            everything it writes is marked as a draft for review.
          </p>
          <p>
            <strong>In total:</strong> on Cloudflare, about $3 to $45 once, then about $6 a month at almost any traffic, because R2 charges
            nothing for egress. On AWS, about $4 to $25 once, then about $1 a month until readers pull more than 1 TB (about 4 million
            clicks), and roughly $25 for each further million clicks.
          </p>
          <h3>What changes the numbers</h3>
          <ul className="list-disc space-y-2 pl-6">
            <li>Egress is the only cost that grows with readers. A click on a transcript sends about 0.25 MB; the original PDFs here run from 0.1 MB to 16 MB, so serving the transcript instead of the PDF also cuts egress.</li>
            <li>OCR was not installed for the measurement, so the scan figures use a typical rate for OCRmyPDF with Tesseract.</li>
            <li>An enterprise contract (Cloudflare Enterprise, AWS enterprise support) is a negotiated fee, often thousands of dollars a month, that buys SLAs and support rather than capacity.</li>
            <li>A converter upgrade means converting everything again, at the one-time cost. Keying the cache on the PDF&rsquo;s hash and the converter version keeps unchanged documents from being redone between upgrades.</li>
            <li>AI assist needs a GPU, which Cloudflare does not offer, so that part runs on a rented GPU billed by the second, or on an owned card: the whole 150,000-page batch is about 5 days on one RTX 3090, or about a day with batching.</li>
            <li>The work a person does on the review queue is not included, and it costs more than the computers.</li>
          </ul>
        </section>

        <section id="accessibility" aria-labelledby="a11y" className="flex flex-col gap-4">
          <h2 id="a11y">Accessibility statement</h2>
          <p>This site aims to meet the Web Content Accessibility Guidelines (WCAG) 2.2 at level AA. It follows your system&rsquo;s light or dark setting, works with a keyboard alone, keeps text resizable and reduces motion when you ask your system to.</p>
          <h3>Known limitations</h3>
          <ul className="list-disc space-y-2 pl-6">
            <li>Original page images are pictures of the page. Use the transcript to read them.</li>
            <li>Transcripts are generated automatically. Parts reflow is unsure of are listed for review and may read imperfectly until someone checks them.</li>
            <li>Some older conversions do not link transcript blocks to boxes in the inspector.</li>
          </ul>
        </section>

        <section id="keyboard" aria-labelledby="keys" className="flex flex-col gap-4">
          <h2 id="keys">Keyboard shortcuts</h2>
          <p className="text-muted-foreground">
            The transcript&rsquo;s shortcuts are Alt and Shift with a letter (Option and Shift on a Mac), so they never replace typing; only the ones that move you out of a field work while you type. The inspector&rsquo;s single-key shortcuts do nothing while you type in a field.
          </p>
          <div className="overflow-hidden rounded-xl ring-1 ring-foreground/10">
            <table className="w-full text-left type-small">
              <caption className="sr-only">Keyboard shortcuts</caption>
              <thead className="bg-muted/50">
                <tr>
                  <th scope="col" className="h-11 px-4 type-label">Keys</th>
                  <th scope="col" className="h-11 px-4 type-label">Action</th>
                  <th scope="col" className="h-11 px-4 type-label">Where</th>
                </tr>
              </thead>
              <tbody>
                {SHORTCUTS.map((s) => (
                  <tr key={`${s.where}:${s.action}`} className="border-t">
                    <td className="px-4 py-3"><span className="flex flex-wrap items-center gap-1">{s.keys.map((k, i) => <span key={k} className="inline-flex items-center gap-1">{i > 0 && s.keys.length === 3 ? <span aria-hidden="true">+</span> : null}<Kbd>{k}</Kbd></span>)}</span></td>
                    <td className="px-4 py-3">{s.action}</td>
                    <td className="px-4 py-3 text-muted-foreground">{s.where}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </>
  );
}
