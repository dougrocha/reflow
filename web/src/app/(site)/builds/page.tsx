import { BuildsView } from "@/components/builds/builds-view";
import { PageHeader } from "@/components/app/page-header";

export const metadata = { title: "Builds" };

// Every build of a document, from this page or from `bun run docs:build`: the queue, what is
// running with its progress, and the latest finished ones. Only when builds are on: proxy.ts answers
// a real 404 otherwise (docs/builds.md).
export default function BuildsPage() {
  return (
    <>
      <PageHeader
        title="Builds"
        lead="Document builds that are queued, running or finished, whether you started them here or from the command line."
      />
      <BuildsView />
    </>
  );
}
