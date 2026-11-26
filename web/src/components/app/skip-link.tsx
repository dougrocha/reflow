export function SkipLink() {
  return (
    <a
      href="#main"
      className="sr-only rounded-lg bg-background px-4 py-2 font-medium text-foreground shadow-lg ring-1 ring-foreground/10 focus:not-sr-only focus:fixed focus:px-4 focus:py-2 focus:top-2 focus:left-2 focus:z-[100]"
    >
      Skip to main content
    </a>
  );
}
