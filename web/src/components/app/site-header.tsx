import { Suspense } from "react";
import Link from "next/link";
import { FileText } from "lucide-react";
import { CONTAINER } from "./layout";
import { MainNav } from "./main-nav";
import { cn } from "@/lib/utils";

export function SiteHeader() {
  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 supports-backdrop-filter:bg-background/80 supports-backdrop-filter:backdrop-blur">
      <div className={cn(CONTAINER, "flex h-14 items-center gap-2 sm:gap-6")}>
        <Link href="/" aria-label="reflow home" className="flex shrink-0 items-center gap-2 rounded-lg font-semibold text-foreground">
          <FileText aria-hidden="true" className="size-5" />
          <span aria-hidden="true">reflow</span>
        </Link>
        {/* usePathname can suspend under cacheComponents; the fallback is the same nav without a current item. */}
        <Suspense fallback={<MainNav static />}>
          <MainNav />
        </Suspense>
      </div>
    </header>
  );
}
