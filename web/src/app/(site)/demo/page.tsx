import { Suspense } from "react";
import { connection } from "next/server";
import { PageHeader } from "@/components/app/page-header";
import { Kbd } from "@/components/ui/kbd";
import { buttonVariants } from "@/components/ui/button-variants";
import { DEMO_BASE, DEMO_SITE_KEY, demoOrigin } from "@/lib/resolver/demo";
import { ReflowLinks } from "@/reflow/react";

export const metadata = { title: "Demo" };

const SNIPPET = `<script src="https://<reflow origin>/embed/loader.js" data-site="KEY" defer></script>`;

function Snippet({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-xl bg-muted p-4 type-mono" tabIndex={0} role="region" aria-label="Code example">
      <code>{children}</code>
    </pre>
  );
}

/** The link to the customer page, or how to start it. REFLOW_DEMO_ORIGIN is read per request: it
 *  is the origin scripts/dev gives the demo page, and it is not known at build time. */
async function TryIt() {
  await connection();
  const origin = demoOrigin();
  if (origin === null) {
    return (
      <p>
        The customer page is not running. Start the demo servers with{" "}
        <code className="rounded-md bg-muted px-1.5 py-0.5 type-mono">scripts/dev start</code>; it
        tells this server the page&rsquo;s origin.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <p>
        The customer page is served by this server on another origin,{" "}
        <code className="type-mono">{origin}</code>, so the loader runs across origins as it does on a
        real site. It opens in this tab; use Back to return here.
      </p>
      <p>
        <a href={`${origin}${DEMO_BASE}`} className={buttonVariants({ size: "lg" })}>Open the customer page</a>
      </p>
    </div>
  );
}

export default function DemoPage() {
  return (
    <>
      <PageHeader
        title="Try the embed"
        lead="A site adds one script. Its links to PDFs keep working as links, and a click opens reflow's accessible version of the document in a dialog on the site's own page."
      />
      <div className="flex max-w-3xl flex-col gap-12">
        <section aria-labelledby="snippet" className="flex flex-col gap-4">
          <h2 id="snippet">The snippet</h2>
          <p>
            A customer adds this to a page, with the origin where reflow runs and the key the operator
            registered for the site (<code className="type-mono">docs/resolver.md</code>):
          </p>
          <Snippet>{SNIPPET}</Snippet>
          <p>
            The demo site&rsquo;s key is <code className="type-mono">{DEMO_SITE_KEY}</code>. Nothing else on the
            page changes: the links stay what they were, and without the script, or when it
            fails, they go to their PDFs.
          </p>
        </section>

        <section aria-labelledby="click" className="flex flex-col gap-4">
          <h2 id="click">What happens on a click</h2>
          <ol className="list-decimal space-y-2 pl-6">
            <li>The loader holds the click and asks reflow&rsquo;s resolver whether this PDF has a converted document for this site.</li>
            <li>If it does, a dialog opens on the page with the document&rsquo;s viewer in a frame, named after the document, and focus moves into it.</li>
            <li>
              <Kbd>Esc</Kbd> or the viewer&rsquo;s Close button closes the dialog, as does the browser&rsquo;s Back button.
              Focus returns to the link and the page is where it was.
            </li>
            <li>If the resolver does not know the link, is slow or fails, the browser goes to the PDF, as it would without the script. Ctrl-click and middle-click always do.</li>
          </ol>
        </section>

        <section aria-labelledby="try" className="flex flex-col gap-4">
          <h2 id="try">Try it</h2>
          <Suspense fallback={<p aria-busy="true" className="text-muted-foreground">Looking for the customer page&hellip;</p>}>
            <TryIt />
          </Suspense>
          <p className="text-muted-foreground">
            It has links to two registered PDFs, which open here, and to one the site did not register, which goes to the PDF.
          </p>
        </section>

        <section aria-labelledby="embed-demo" className="flex flex-col gap-4">
          <h2 id="embed-demo">The React way</h2>
          <p>
            Inside this app, <code className="type-mono">ReflowLinks</code> does the same for the links it wraps, with no
            script and no frame: the document opens in a dialog of this page. These PDF links open accessible transcripts that way.
          </p>
          <ReflowLinks documents={{ "/pdfs/fw9.pdf": "/docs/fw9/", "/pdfs/resnet.pdf": "/docs/resnet/" }}>
            <ul className="list-disc space-y-2 pl-6">
              <li><a href="/pdfs/fw9.pdf" className="underline underline-offset-4">Form W-9 (PDF)</a></li>
              <li><a href="/pdfs/resnet.pdf" className="underline underline-offset-4">Deep Residual Learning for Image Recognition (PDF)</a></li>
            </ul>
          </ReflowLinks>
        </section>
      </div>
    </>
  );
}
