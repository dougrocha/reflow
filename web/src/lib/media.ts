// Whether a CSS media query matches, read once or followed as React state.

import { useCallback, useSyncExternalStore } from "react";

/** Whether `query` matches now. Browser only. */
export const matchesMedia = (query: string): boolean => window.matchMedia(query).matches;

/** Whether `query` matches, re-rendering when that changes. The server render, which has no
 *  screen, takes it as matching. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    const mq = window.matchMedia(query);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [query]);
  return useSyncExternalStore(subscribe, () => matchesMedia(query), () => true);
}
