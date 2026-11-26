"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { overviewUrl } from "@/lib/urls";

// /#<slug> was the old dashboard anchor: send it to the document overview. #reflow=… belongs to
// ReflowLinks (the open viewer dialog) and is left alone.
export function HashRedirect({ slugs }: { slugs: string[] }) {
  const router = useRouter();
  useEffect(() => {
    const go = () => {
      let hash: string;
      try {
        hash = decodeURIComponent(window.location.hash.slice(1));
      } catch {
        return; // a malformed escape such as #% is not a slug
      }
      if (hash && !hash.startsWith("reflow=") && slugs.includes(hash)) router.replace(overviewUrl(hash));
    };
    go();
    window.addEventListener("hashchange", go);
    return () => window.removeEventListener("hashchange", go);
  }, [router, slugs]);
  return null;
}
