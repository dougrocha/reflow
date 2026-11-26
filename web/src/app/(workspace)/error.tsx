"use client";

import { lazy, Suspense } from "react";

// React.lazy, not next/dynamic: this file is in every workspace page's first load (it is the
// route's error boundary), and the error UI itself is not.
const ErrorState = lazy(() => import("@/components/app/error-state").then((m) => ({ default: m.ErrorState })));

export default function WorkspaceError({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return (
    <div className="grid flex-1 place-items-center p-6">
      <Suspense fallback={null}>
        <ErrorState
          title="This document could not be opened"
          description="Something went wrong while loading it. Try again, or go back to the document list."
          onRetry={retry}
          homeHref="/"
        />
      </Suspense>
    </div>
  );
}
