import { CircleCheck, CircleDashed } from "lucide-react";
import { Badge } from "@/components/ui/badge";

export function StatusBadge({ converted }: { converted: boolean }) {
  return converted ? (
    <Badge variant="outline" className="h-6 gap-1 border-success/40 bg-success-subtle px-2 type-caption font-medium text-success">
      <CircleCheck aria-hidden="true" />
      Converted
    </Badge>
  ) : (
    <Badge variant="outline" className="h-6 gap-1 px-2 type-caption font-medium text-muted-foreground">
      <CircleDashed aria-hidden="true" />
      Not converted
    </Badge>
  );
}
