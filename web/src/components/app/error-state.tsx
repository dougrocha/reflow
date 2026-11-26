"use client";

import Link from "next/link";
import { CircleAlert, RotateCw } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/native-button";

export function ErrorState({
  title = "Something went wrong",
  description,
  onRetry,
  homeHref,
  homeLabel = "Go to documents",
}: {
  title?: string;
  description?: React.ReactNode;
  onRetry?: () => void;
  homeHref?: string;
  homeLabel?: string;
}) {
  return (
    <div role="alert" className="mx-auto flex max-w-lg flex-col items-center gap-3 rounded-xl border border-severity-error/40 bg-severity-error-subtle p-8 text-center">
      <CircleAlert aria-hidden="true" className="size-8 text-severity-error" />
      <h2 className="type-h3">{title}</h2>
      {description ? <p className="type-small text-foreground">{description}</p> : null}
      <div className="mt-2 flex flex-wrap justify-center gap-2">
        {onRetry ? (
          <Button variant="outline" onClick={onRetry}>
            <RotateCw aria-hidden="true" />
            Try again
          </Button>
        ) : null}
        {homeHref ? <Link href={homeHref} className={buttonVariants({ variant: "ghost" })}>{homeLabel}</Link> : null}
      </div>
    </div>
  );
}
