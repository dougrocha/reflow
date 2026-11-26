import { existsSync } from "node:fs";
import { chromium, type Browser, type LaunchOptions } from "playwright";

/** A page card (data-page) holds its transcript (data-transcript); the card exists before its text,
 *  so this is what shows the viewer has rendered a page. */
export const TRANSCRIPT_READY = "[data-page] [data-transcript]";

/** Launches Chromium: $CHROME_PATH, else /usr/bin/chromium, else Playwright's own. */
export function launchChromium(options: LaunchOptions = {}): Promise<Browser> {
  const executablePath = process.env.CHROME_PATH ?? (existsSync("/usr/bin/chromium") ? "/usr/bin/chromium" : undefined);
  return chromium.launch({ executablePath, ...options });
}
