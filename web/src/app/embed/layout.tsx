// The embeddable viewer, alone on the page: no site header or footer. It fills the frame another
// site opens it in (docs/embed.md), and panels scroll inside, as in the workspace.
export default function EmbedLayout({ children }: { children: React.ReactNode }) {
  return <div className="flex h-dvh flex-col overflow-hidden bg-background text-foreground">{children}</div>;
}
