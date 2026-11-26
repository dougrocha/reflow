// Type guards for values parsed from JSON (request bodies, stored records, model replies).

/** A plain object: not null, not an array. */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** A number that is not NaN or infinite. */
export function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}
