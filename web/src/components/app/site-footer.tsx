import Link from "next/link";
import { CONTAINER } from "./layout";
import { cn } from "@/lib/utils";

export function SiteFooter() {
  return (
    <footer className="border-t">
      <div className={cn(CONTAINER, "flex flex-col gap-2 py-8 type-small text-muted-foreground sm:flex-row sm:items-center sm:gap-3")}>
        <p>reflow turns PDFs into accessible HTML transcripts.</p>
        <span aria-hidden="true" className="hidden sm:inline">·</span>
        <Link href="/about#accessibility" className="underline-offset-4 hover:underline">Accessibility statement</Link>
        <span aria-hidden="true" className="hidden sm:inline">·</span>
        <Link href="/about#keyboard" className="underline-offset-4 hover:underline">Keyboard shortcuts</Link>
      </div>
    </footer>
  );
}
