import type { ReactNode } from "react";
import Link from "next/link";
import { ArrowLeft, FileText } from "lucide-react";
import { buttonVariants } from "@/components/ui/button-variants";

import { variantOf } from "@/pipelines";
import type { BuildInfo } from "@/lib/build-info";
import { BuildInfoButton } from "./build-info";
import type { VariantTab } from "@/lib/variants/view";
import { VariantTabs } from "./variant-tabs";
import { ViewSwitch, type WorkspaceView } from "./view-switch";
import { overviewUrl } from "@/lib/urls";

export function WorkspaceHeader({
  slug, title, current, viewHref, inspectHref, build, variants, page = null, children,
}: {
  slug: string;
  title: string;
  current: WorkspaceView;
  viewHref: string;
  inspectHref: string;
  /** How the transcript was made (null: a build from before it was recorded); undefined for a
   *  document with no transcript, which gets no button. */
  build?: BuildInfo | null;
  /** The document's built variants: with them, the tab strip and the build dropdown show
   *  under the header. `page` is kept when switching tabs. */
  variants?: VariantTab[];
  page?: number | null;
  /** Right-hand controls (Help, More…). */
  children?: ReactNode;
}) {
  // A variant's overview is its document's (/docs takes no variant keys).
  const doc = variantOf(slug)?.doc.slug ?? slug;
  const suffix = current === "view" ? ": accessible transcript" : ": inspector";
  return (
    <>
    <header className="flex h-12 shrink-0 items-center gap-2 border-b bg-background px-2 sm:px-3">
      {/* Phones: back to the overview. */}
      <Link prefetch={false} href={overviewUrl(doc)} aria-label={`Back to ${title} overview`}
            className={buttonVariants({ variant: "ghost", size: "icon-lg", className: "md:hidden" })}>
        <ArrowLeft aria-hidden="true" />
      </Link>
      {/* md+: home, then Documents / title. */}
      <Link prefetch={false} href="/" aria-label="reflow home" className={buttonVariants({ variant: "ghost", size: "icon-lg", className: "max-md:hidden" })}>
        <FileText aria-hidden="true" />
      </Link>
      <div role="separator" aria-orientation="vertical" className="hidden h-5 w-px shrink-0 bg-border md:block" />
      <nav aria-label="Breadcrumb" className="hidden shrink-0 md:block">
        <ol role="list" className="flex items-center gap-1.5 type-small text-muted-foreground">
          <li><Link prefetch={false} href="/" className="text-muted-foreground hover:text-foreground">Documents</Link></li>
          <li aria-hidden="true">/</li>
        </ol>
      </nav>
      <h1 className="relative min-w-0 flex-1 truncate type-label font-semibold" title={title}>
        <Link prefetch={false} href={overviewUrl(doc)} className="hidden text-foreground hover:underline md:inline">{title}</Link>
        <span className="md:hidden">{title}</span>
        <span className="sr-only">{suffix}</span>
      </h1>
      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        {build !== undefined ? <BuildInfoButton build={build} slug={slug} /> : null}
        <ViewSwitch current={current} viewHref={viewHref} inspectHref={inspectHref} />
        {children}
      </div>
    </header>
    {variants ? <VariantTabs slug={doc} active={slug} built={variants} view={current} page={page} /> : null}
    </>
  );
}
