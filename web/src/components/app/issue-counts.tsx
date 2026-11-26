import Link from "next/link";
import { CircleCheck } from "lucide-react";
import { SeverityBadge } from "./severity-badge";

/** Error and warning badges; "No issues" when both are 0. With href, the badges link there. */
export function IssueCounts({ errors, warnings, href }: { errors: number; warnings: number; href?: string }) {
  if (errors === 0 && warnings === 0) {
    return (
      <span className="inline-flex items-center gap-1 type-small text-success">
        <CircleCheck aria-hidden="true" className="size-4" />
        No issues
      </span>
    );
  }
  const badges = (
    <>
      {errors > 0 ? <SeverityBadge severity="error" count={errors} /> : null}
      {warnings > 0 ? <SeverityBadge severity="warning" count={warnings} /> : null}
    </>
  );
  return href ? (
    <Link href={href} className="inline-flex flex-wrap gap-1.5 rounded-full text-inherit">{badges}</Link>
  ) : (
    <span className="inline-flex flex-wrap gap-1.5">{badges}</span>
  );
}
