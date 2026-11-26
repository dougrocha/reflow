"use client";

import Link from "next/link";
import { buttonVariants } from "@/components/ui/button-variants";
import { useBuilds } from "./context";

/** A link to the builds page, with how many are queued or running. Nothing when builds are off. */
export function BuildsLink() {
  const builds = useBuilds();
  if (!builds) return null;
  const n = builds.data?.active.length ?? 0;
  return (
    <Link href="/builds" className={buttonVariants({ variant: "outline", size: "sm" })}>
      Builds
      {n > 0 ? <span className="sr-only">, {n} active</span> : null}
      {n > 0 ? <span aria-hidden="true" className="tabular-nums">({n})</span> : null}
    </Link>
  );
}
