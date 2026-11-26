import { Skeleton } from "@/components/ui/skeleton";

export function WorkspaceSkeleton({ variant }: { variant: "view" | "inspect" }) {
  return (
    <div aria-busy="true" className="flex min-h-0 flex-1 flex-col">
      <span className="sr-only">{variant === "view" ? "Loading document…" : "Loading the inspector…"}</span>
      <div className="flex h-12 items-center gap-3 border-b px-3">
        <Skeleton className="size-8" />
        <Skeleton className="h-4 w-64 max-w-[50%]" />
        <Skeleton className="ml-auto h-8 w-48 max-md:hidden" />
      </div>
      <div className="flex h-11 items-center gap-2 border-b px-3">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-8 w-32" />
      </div>
      {variant === "view" ? (
        <div className="flex min-h-0 flex-1 bg-muted/40">
          <div className="hidden w-72 flex-col gap-3 border-r bg-card p-5 lg:flex">
            {Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-4 w-full" />)}
          </div>
          <div className="flex flex-1 justify-center p-4"><Skeleton className="h-full w-full max-w-3xl rounded-xl" /></div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="flex flex-[62] items-start justify-center bg-muted/40 p-4"><Skeleton className="aspect-[17/22] w-full max-w-2xl" /></div>
          <div className="hidden flex-[38] flex-col gap-3 border-l p-4 md:flex">
            <Skeleton className="h-6 w-1/2" />
            {Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-4 w-full" />)}
          </div>
        </div>
      )}
    </div>
  );
}
