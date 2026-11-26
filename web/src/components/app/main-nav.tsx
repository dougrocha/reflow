"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "/", label: "Documents", match: (p: string) => p === "/" || p.startsWith("/docs/") },
  { href: "/review", label: "Review", match: (p: string) => p.startsWith("/review") },
  { href: "/demo", label: "Demo", match: (p: string) => p.startsWith("/demo") },
  { href: "/about", label: "About", match: (p: string) => p.startsWith("/about") },
];

function NavList({ pathname }: { pathname: string | null }) {
  return (
    <nav aria-label="Main">
      <ul role="list" className="flex items-center gap-0.5 sm:gap-1">
        {ITEMS.map((item) => (
          <li key={item.href}>
            <Link
              href={item.href}
              aria-current={pathname && item.match(pathname) ? "page" : undefined}
              className="inline-flex h-9 items-center rounded-lg px-2 text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground aria-[current=page]:bg-muted aria-[current=page]:text-foreground sm:px-3"
            >
              {item.label}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function CurrentNav() {
  return <NavList pathname={usePathname()} />;
}

export function MainNav({ static: isStatic = false }: { static?: boolean }) {
  return isStatic ? <NavList pathname={null} /> : <CurrentNav />;
}
