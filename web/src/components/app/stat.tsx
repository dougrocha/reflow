import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

const TONE = {
  default: "text-foreground",
  error: "text-severity-error",
  warning: "text-severity-warning",
  success: "text-success",
} as const;

export function StatGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={cn("grid grid-cols-2 gap-3 md:grid-cols-4", className)}>{children}</dl>;
}

export function Stat({ label, value, tone = "default", hint }: {
  label: string; value: ReactNode; tone?: keyof typeof TONE; hint?: string;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-xl p-4 ring-1 ring-foreground/10">
      <dt className="type-label text-muted-foreground">{label}</dt>
      <dd className={cn("text-2xl font-semibold tabular-nums", TONE[tone])}>{value}</dd>
      {hint ? <dd className="type-caption text-muted-foreground">{hint}</dd> : null}
    </div>
  );
}
