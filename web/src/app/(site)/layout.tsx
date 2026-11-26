import { SiteHeader } from "@/components/app/site-header";
import { SiteFooter } from "@/components/app/site-footer";
import { CONTAINER } from "@/components/app/layout";
import { cn } from "@/lib/utils";

// Content pages: the site header and footer around a centred container.
export default function SiteLayout({ children }: { children: React.ReactNode }) {
  return (
    <div data-site-shell className="flex min-h-dvh flex-col">
      <SiteHeader />
      <main id="main" tabIndex={-1} className="flex-1 focus:outline-none">
        <div className={cn(CONTAINER, "py-8 sm:py-10 lg:py-12")}>{children}</div>
      </main>
      <SiteFooter />
    </div>
  );
}
