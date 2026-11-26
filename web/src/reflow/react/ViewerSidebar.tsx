"use client";

import { Download, Printer } from "lucide-react";
import { type TocEntry } from "../core";
import { type ViewMode } from "./useReflowDocument";
import { Button, buttonVariants } from "@/components/ui/native-button";
import { pageCountText } from "@/lib/pages";
import { AccordionSection } from "./SidebarAccordion";
import { type SectionId, type Sections } from "./sidebar-sections";

// The viewer's sidebar: the contents (Outline), the Ask section's place and the document's actions.

const TOC_TITLE = "Table of contents";
const ASK_TITLE = "Ask a question";
/** What the column, the sheet and the button that opens the sheet are called. */
export const sidebarLabel = (ask: boolean) => (ask ? `${TOC_TITLE} and ${ASK_TITLE.toLowerCase()}` : TOC_TITLE);
/**
 * The sidebar: an accordion of "Table of contents" and, for a document that can answer, "Ask a
 * question", with the download, print and load-all buttons under it, outside any item and always
 * shown. Rendered twice, in the column and in the sheet; only one shows. Each open item scrolls on
 * its own: with Ask in the accordion the outline is capped (45vh while Ask is open, 55vh when it is
 * not) and the answer is capped at 50vh, so a long outline can neither hide the "Ask a question"
 * header nor push the form or its answer out of reach; in the column the accordion scrolls as
 * a whole above the buttons, and the column itself only when the window is too short even for that.
 * The Ask item's body is an empty host element: the lazily loaded controller draws the form into it.
 */
export function Sidebar({ where, id, contents, onPick, sections, setSection, askEnabled, onAskOpen, askHeader, askHost }: {
  where: "column" | "sheet";
  id: string;
  contents: SidebarContents;
  onPick: (page: number, id: string, text: string) => void;
  sections: Sections;
  setSection: (id: SectionId, open: boolean) => void;
  askEnabled: boolean;
  /** The Ask item was opened by hand: time to load its controller. */
  onAskOpen: () => void;
  askHeader: React.Ref<HTMLButtonElement>;
  askHost: React.Ref<HTMLDivElement>;
}) {
  const base = `${id}-${where}`;
  return (
    <>
      <div className={where === "column" ? "min-h-48 flex-1 overflow-y-auto" : ""}>
        <AccordionSection id={`${base}-toc`} title={TOC_TITLE} open={sections.toc}
                          onToggle={() => setSection("toc", !sections.toc)}
                          panelClassName={askEnabled ? (sections.ask ? "max-h-[45vh] overflow-y-auto" : "max-h-[55vh] overflow-y-auto") : ""}>
          <Outline {...contents} onPick={onPick} />
        </AccordionSection>
        {askEnabled ? (
          <AccordionSection id={`${base}-ask`} title={ASK_TITLE} open={sections.ask} buttonRef={askHeader} keyShortcuts="Alt+Shift+A"
                            onToggle={() => { if (!sections.ask) onAskOpen(); setSection("ask", !sections.ask); }}>
            {/* Empty for React: the controller draws the form into it. */}
            <div ref={askHost} />
          </AccordionSection>
        ) : null}
      </div>
      <div className="shrink-0 border-t p-5">
        <Actions {...contents} />
      </div>
    </>
  );
}

const RTL_LETTER = /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/;

type OutlineProps = {
  pageCount: number | null;
  outline: TocEntry[];
  tocComplete: boolean;
  /** The document's language and direction: the entries are its headings. A bilingual
   *  right-to-left document can have left-to-right entries: they take `ltrLang` (the
   *  language its left-to-right text reads as) when there is one, else the viewer's. */
  lang?: string;
  dir?: "ltr" | "rtl";
  ltrLang?: string;
};
type ActionsProps = {
  pageCount: number | null;
  original: string | null;
  allLoaded: boolean;
  loadingAll: boolean;
  onLoadAll: () => void;
  mode: ViewMode;
  downloading: boolean;
  onDownloadTranscript: () => void;
  printing: boolean;
  onPrintTranscript: () => void;
  /** The document has fillable form fields and an original PDF to fill. */
  fillable: boolean;
  fillingPdf: "download" | "print" | null;
  fillMessage: string;
  onFillPdf: (how: "download" | "print") => void;
};
type SidebarContents = OutlineProps & ActionsProps;

/** The table of contents: the page count and the outline, the body of its accordion item. */
function Outline({ pageCount, outline, tocComplete, lang, dir, ltrLang, onPick }: OutlineProps & {
  onPick: (page: number, id: string, text: string) => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      {pageCount ? <p className="type-small text-muted-foreground">{pageCountText(pageCount)}</p> : null}
      {outline.length > 0 ? (
        <nav aria-label="Document outline">
          <ol role="list" className="space-y-0.5 type-small">
            {outline.map((e) => (
              <li key={e.id} lang={dir === "rtl" && !RTL_LETTER.test(e.text) ? ltrLang : lang} dir="auto" style={{ paddingInlineStart: `${Math.max(0, e.level - 2) * 0.9}rem` }}>
                <a href={`#${e.id}`} className="inline-block rounded-sm py-1 underline-offset-4 hover:underline"
                   onClick={(ev) => { ev.preventDefault(); onPick(e.page, e.id, e.text); }}>
                  {e.text}
                </a>
              </li>
            ))}
          </ol>
          {!tocComplete ? (
            <p className="mt-3 type-small text-muted-foreground">
              Only the main sections are listed. Use &ldquo;Load entire document&rdquo; and your browser&rsquo;s find, or a screen reader&rsquo;s heading list, to reach every entry.
            </p>
          ) : null}
        </nav>
      ) : null}
    </div>
  );
}

/** Download, print and load-all: not part of the accordion, always shown under it. */
function Actions({
  pageCount, original, allLoaded, loadingAll, onLoadAll,
  mode, downloading, onDownloadTranscript, printing, onPrintTranscript, fillable, fillingPdf, fillMessage, onFillPdf,
}: ActionsProps) {
  return (
    <div className="flex flex-col gap-4">
      {/* The actions follow the view: what you are reading is what you download or print. */}
      <div className="grid gap-2">
        {mode === "transcript" ? (
          <>
            <Button type="button" onClick={onDownloadTranscript} disabled={downloading || !pageCount}>
              <Download aria-hidden="true" />
              Download transcript (HTML)
            </Button>
            {allLoaded ? (
              <Button type="button" variant="outline" onClick={onPrintTranscript} disabled={printing || !pageCount}>
                <Printer aria-hidden="true" />
                Print transcript (HTML)
              </Button>
            ) : pageCount ? (
              <p className="type-small text-muted-foreground">Printing is offered once the entire document is loaded.</p>
            ) : null}
            {fillable ? (
              <>
                <Button type="button" onClick={() => onFillPdf("download")} disabled={!!fillingPdf}>
                  <Download aria-hidden="true" />
                  Download filled PDF
                </Button>
                <Button type="button" variant="outline" onClick={() => onFillPdf("print")} disabled={!!fillingPdf}>
                  <Printer aria-hidden="true" />
                  Print filled PDF<span className="sr-only">, opens the filled PDF in a new tab, where you can print it</span>
                </Button>
                {fillMessage ? <p className="type-small text-muted-foreground">{fillMessage}</p> : null}
              </>
            ) : null}
          </>
        ) : original ? (
          <>
            <a className={buttonVariants()} href={original} download>
              <Download aria-hidden="true" />
              Download original PDF
            </a>
            <a className={buttonVariants({ variant: "outline" })} href={original} target="_blank" rel="noopener">
              <Printer aria-hidden="true" />
              Print PDF<span className="sr-only">, opens the PDF in a new tab, where you can print it</span>
            </a>
          </>
        ) : null}
        {pageCount && !allLoaded ? (
          <Button type="button" variant="outline" onClick={onLoadAll} disabled={loadingAll}>
            Load entire document
          </Button>
        ) : null}
      </div>
    </div>
  );
}
