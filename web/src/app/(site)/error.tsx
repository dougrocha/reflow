"use client";

import { ErrorState } from "@/components/app/error-state";

export default function SiteError({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <ErrorState
      title="This page could not be shown"
      description="Something went wrong while reading the documents. Try again, or go back to the document list."
      onRetry={retry}
      homeHref="/"
    />
  );
}
