"use client";

import { useCallback, useMemo, useSyncExternalStore } from "react";

// Settings kept per browser. Read through useSyncExternalStore, so the server render and
// the first client render agree on the fallback, and every write reaches every hook using the
// same key. localStorage can throw (private windows, blocked storage, quota), so every access is
// wrapped: a write that fails is kept in memory, and the setting lasts for this visit.
// A setting is kept per browser ("local", the default) or only for the tab's session ("session":
// it survives a reload and is gone when the tab closes).

export const PERSIST_EVENT = "reflow:persist";

type StorageArea = "local" | "session";

const unsaved = new Map<string, string>();
const slot = (key: string, area: StorageArea) => (area === "session" ? `session:${key}` : key);
const areaOf = (area: StorageArea) => (area === "session" ? sessionStorage : localStorage);

export function readStored(key: string, area: StorageArea = "local"): string | null {
  const k = slot(key, area);
  if (unsaved.has(k)) return unsaved.get(k) ?? null;
  try {
    return areaOf(area).getItem(key);
  } catch {
    return null;
  }
}

export function writeStored(key: string, raw: string, area: StorageArea = "local"): void {
  const k = slot(key, area);
  try {
    areaOf(area).setItem(key, raw);
    unsaved.delete(k);
  } catch {
    unsaved.set(k, raw);
  }
  window.dispatchEvent(new CustomEvent(PERSIST_EVENT, { detail: key }));
}

export function removeStored(key: string, area: StorageArea = "local"): void {
  unsaved.delete(slot(key, area));
  try {
    areaOf(area).removeItem(key);
  } catch { /* storage unavailable */ }
}

function subscribe(key: string) {
  return (onChange: () => void) => {
    const handler = (e: Event) => {
      if (e instanceof CustomEvent && e.detail === key) onChange();
    };
    window.addEventListener(PERSIST_EVENT, handler);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(PERSIST_EVENT, handler);
      window.removeEventListener("storage", onChange);
    };
  };
}

/**
 * A persisted setting. `fallback` must be a module-level constant (its identity is used to
 * memoize). `validate` turns the stored JSON into the value, or null when it does not fit.
 */
export function usePersisted<T>(key: string, fallback: T, validate: (raw: unknown) => T | null, area: StorageArea = "local") {
  const subscribeKey = useMemo(() => subscribe(key), [key]);
  const raw = useSyncExternalStore(subscribeKey, () => readStored(key, area), () => null);
  const value = useMemo(() => {
    if (raw === null) return fallback;
    try {
      return validate(JSON.parse(raw)) ?? fallback;
    } catch {
      return fallback;
    }
  }, [raw, fallback, validate]);

  const set = useCallback((next: T) => writeStored(key, JSON.stringify(next), area), [key, area]);

  return [value, set] as const;
}
