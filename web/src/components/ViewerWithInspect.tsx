"use client";

import { useEffect, useState } from "react";
// The viewer itself, not the package index: that also exports ReflowLinks (the dialog viewer
// for the about page), which would bring its dialog code into this page's first load.
import { DocumentViewer } from "@/reflow/react/DocumentViewer";
import { WorkspaceHeader } from "@/components/app/workspace-header";

import type { BuildInfo } from "@/lib/build-info";
import type { VariantTab } from "@/lib/variants/view";
import { inspectUrl, viewerUrl } from "@/lib/urls";

// The /view page: the workspace header (whose view switch follows the page being read) and the viewer.
export function ViewerWithInspect({ slug, title, base, initialPage, askReady, build, variants }: {
  slug: string; title: string; base: string; initialPage?: number;
  /** How the transcript was made (the header's panel). */
  build: BuildInfo | null;
  /** The document's built variants, for the tab strip. */
  variants: VariantTab[];
  /** The document can answer questions (askReady on the server): the viewer then gets the Ask button. */
  askReady?: boolean;
}) {
  const [current, setCurrent] = useState(initialPage ?? 1);
  useEffect(() => {
    const next = viewerUrl({ slug, page: current });
    if (window.location.pathname + window.location.search + window.location.hash !== next) {
      window.history.replaceState(null, "", next);
    }
  }, [slug, current]);

  return (
    <>
      <WorkspaceHeader
        slug={slug}
        title={title}
        current="view"
        viewHref={viewerUrl({ slug, page: current })}
        inspectHref={inspectUrl({ slug, page: current })}
        build={build}
        variants={variants}
        page={current}
      />
      <main id="main" tabIndex={-1} className="flex min-h-0 flex-1 flex-col focus:outline-none">
        <DocumentViewer base={base} documentTitle={title} initialPage={initialPage} onPageChange={setCurrent}
                        pageSourceUrl={(page) => `/api/source/${slug}/${page}`}
                        askUrl={askReady ? `/api/ask/${slug}` : undefined} />
      </main>
    </>
  );
}
