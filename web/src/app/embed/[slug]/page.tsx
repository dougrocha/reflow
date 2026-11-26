import { Suspense } from "react";
import { notFound } from "next/navigation";
import { EmbedViewer } from "@/components/EmbedViewer";
import { sampleDoc, slugParams } from "@/docs";
import { readDocument, shownTitle } from "@/lib/docs-data";
import { parsePage } from "@/lib/pages";
import { docBase } from "@/lib/urls";

// /embed/<id>: the viewer that another site frames, opened by the loader from the
// resolver's answer (/v1/resolve), and the same document's shareable hosted page when it is not
// framed. docs/embed.md.
//
// The id is a registered document, a catalogue slug (src/docs.ts): never a base URL or a path, and
// never a variant, so nothing in the URL chooses which HTML this origin loads. Who may frame it is
// decided before this renders, in src/proxy.ts (frame-ancestors from the resolver's index). Ask is
// not offered here: a framed viewer needs a per-site budget for it first.

export const generateStaticParams = slugParams;

export async function generateMetadata(props: PageProps<"/embed/[slug]">) {
  const { slug } = await props.params;
  const doc = sampleDoc(slug);
  if (!doc) return { title: "Document not found" };
  return { title: `${shownTitle(await readDocument(slug), doc)}: transcript` };
}

export default function EmbedPage(props: PageProps<"/embed/[slug]">) {
  return (
    <Suspense fallback={<div className="flex-1" aria-busy="true" />}>
      <Embed params={props.params} searchParams={props.searchParams} />
    </Suspense>
  );
}

async function Embed({ params, searchParams }: {
  params: PageProps<"/embed/[slug]">["params"];
  searchParams: PageProps<"/embed/[slug]">["searchParams"];
}) {
  const { slug } = await params;
  const { page } = await searchParams;
  const doc = sampleDoc(slug);
  if (!doc) notFound();
  const data = await readDocument(slug);
  if (!data.pages) notFound();
  return <EmbedViewer key={data.images.version ?? "unversioned"} slug={slug} title={shownTitle(data, doc)} base={docBase(slug)}
                      initialPage={parsePage(page) ?? undefined} />;
}
