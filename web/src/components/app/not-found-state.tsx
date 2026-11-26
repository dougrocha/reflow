import Link from "next/link";
import { FileQuestion } from "lucide-react";
import { EmptyState } from "@/components/app/empty-state";
import { CONTAINER } from "@/components/app/layout";
import { buttonVariants } from "@/components/ui/button-variants";
import { cn } from "@/lib/utils";

export function NotFoundState() {
  return (
    <div className={cn(CONTAINER, "py-16")}>
      <EmptyState
        icon={FileQuestion}
        headingLevel={1}
        title="Page not found"
        description="There is no page at this address. The document may have been renamed, or it is not in this library."
        action={<Link href="/" className={buttonVariants()}>Go to documents</Link>}
      />
    </div>
  );
}
