import { Suspense } from "react";
import { notFound } from "next/navigation";
import { ViewerWithInspect } from "@/components/ViewerWithInspect";
import { WorkspaceSkeleton } from "@/components/app/workspace-skeleton";
import { NotConvertedWorkspace } from "@/components/app/not-converted";
import { RenderedFrom } from "@/components/builds/rendered-from";
import { slugParams } from "@/docs";
import { variantOf } from "@/pipelines";
import { askReady } from "@/lib/ask";
import { readDocument, shownTitle } from "@/lib/docs-data";
import { builtVariants } from "@/lib/variants/built";
import { parsePage } from "@/lib/pages";
import { docBase } from "@/lib/urls";

export const generateStaticParams = slugParams;

export async function generateMetadata(props: PageProps<"/view/[slug]">) {
  const { slug } = await props.params;
  const doc = variantOf(slug)?.doc;
  if (!doc) return { title: "Document not found" };
  return { title: `${shownTitle(await readDocument(slug), doc)}: transcript` };
}

export default function ViewPage(props: PageProps<"/view/[slug]">) {
  return (
    <Suspense fallback={<WorkspaceSkeleton variant="view" />}>
      <Viewer params={props.params} searchParams={props.searchParams} />
    </Suspense>
  );
}

async function Viewer({ params, searchParams }: {
  params: PageProps<"/view/[slug]">["params"];
  searchParams: PageProps<"/view/[slug]">["searchParams"];
}) {
  const { slug } = await params;
  const { page } = await searchParams;
  const doc = variantOf(slug)?.doc;
  if (!doc) notFound();

  const [data, variants] = await Promise.all([readDocument(slug), builtVariants(doc.slug)]);
  if (!data.pages) return <NotConvertedWorkspace data={data} slug={slug} doc={doc} current="view" page={parsePage(page)} variants={variants} />;

  return (
    <>
      <RenderedFrom reads={[data]} />
      <ViewerWithInspect key={data.images.version ?? "unversioned"} slug={slug} title={shownTitle(data, doc)} base={docBase(slug)} initialPage={parsePage(page) ?? undefined}
                        askReady={await askReady(slug)} build={data.build} variants={variants} />
    </>
  );
}
