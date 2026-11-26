import Link from "next/link";
import { BookOpenText, ScanSearch } from "lucide-react";
import { buttonVariants } from "@/components/ui/button-variants";

export type WorkspaceView = "view" | "inspect";

/** Transcript | Inspector. Segmented from md up; on phones, one icon link to the other view. */
export function ViewSwitch({ current, viewHref, inspectHref }: { current: WorkspaceView; viewHref: string; inspectHref: string }) {
  const items = [
    { key: "view" as const, label: "Transcript", href: viewHref, Icon: BookOpenText },
    { key: "inspect" as const, label: "Inspector", href: inspectHref, Icon: ScanSearch },
  ];
  const other = items.find((i) => i.key !== current)!;
  return (
    <nav aria-label="Document views" className="shrink-0">
      <ul role="list" className="hidden items-center gap-0.5 rounded-lg bg-muted p-0.5 md:flex">
        {items.map(({ key, label, href, Icon }) => (
          <li key={key}>
            <Link
              prefetch={false}
              href={href}
              aria-current={key === current ? "page" : undefined}
              className="inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-sm font-medium text-muted-foreground hover:text-foreground aria-[current=page]:bg-background aria-[current=page]:text-foreground aria-[current=page]:shadow-sm"
            >
              <Icon aria-hidden="true" className="size-4" />
              {label}
            </Link>
          </li>
        ))}
      </ul>
      <Link
        prefetch={false}
        href={other.href}
        aria-label={`Open ${other.label.toLowerCase()}`}
        className={buttonVariants({ variant: "ghost", size: "icon-lg", className: "md:hidden" })}
      >
        <other.Icon aria-hidden="true" />
      </Link>
    </nav>
  );
}
