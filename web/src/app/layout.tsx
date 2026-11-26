import type { Metadata } from "next";
import "./globals.css";
import { Geist, Geist_Mono } from "next/font/google";
import { cn } from "@/lib/utils";
import { SkipLink } from "@/components/app/skip-link";
import { BuildsProvider } from "@/components/builds/builds-provider";

const geist = Geist({ subsets: ["latin"], variable: "--font-sans" });
const geistMono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono" });

export const metadata: Metadata = {
  title: { default: "reflow", template: "%s · reflow" },
  description: "Accessible HTML transcripts of PDF documents",
};

// Build tracking (docs/builds.md) sits here, around the one content tree of every page, so it lasts
// across client navigations between the site and the workspace. It is a client component that
// renders nothing of its own until it has asked the server whether builds are on, so this layout
// reads nothing per request and the pages stay static, the same HTML with builds on or off.
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={cn("font-sans", geist.variable, geistMono.variable)}>
      <body>
        <BuildsProvider>
          <SkipLink />
          {children}
        </BuildsProvider>
      </body>
    </html>
  );
}
