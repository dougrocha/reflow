import { CircleAlert, Info, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";

type Severity = "error" | "warning" | "info";

const STYLE: Record<Severity, { icon: typeof Info; label: string; className: string }> = {
  error: { icon: CircleAlert, label: "Error", className: "border-severity-error/40 bg-severity-error-subtle text-severity-error" },
  warning: { icon: TriangleAlert, label: "Warning", className: "border-severity-warning/40 bg-severity-warning-subtle text-severity-warning" },
  info: { icon: Info, label: "Info", className: "border-severity-info/40 bg-severity-info-subtle text-severity-info" },
};

/** "Error", or with a count "3 errors". Icon and word, never colour alone. */
export function SeverityBadge({ severity, count: n, className }: { severity: Severity; count?: number; className?: string }) {
  const s = STYLE[severity];
  const Icon = s.icon;
  return (
    <Badge variant="outline" className={cn("h-6 gap-1 px-2 type-caption font-medium", s.className, className)}>
      <Icon aria-hidden="true" />
      {n === undefined ? s.label : plural(n, s.label.toLowerCase())}
    </Badge>
  );
}
