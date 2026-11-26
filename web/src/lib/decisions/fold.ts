import type {
  DecisionRecord, Fold, InvalidRecord, Parsed, Reattach, Snapshot, UnsupportedRecord,
} from "./types";

// The fold of docs/review-decisions.md, the same function as `decisions.fold` in the converter
// (src/reflow/analyze/decisions.py). The shared fixtures in tests/fixtures/decisions keep the two
// the same. Read this and that side by side: a change to one is a change to the other.

const roundHalfUp = (x: number): number => Math.floor(x + 0.5);

/** The family keys a record sits on. Each key has its own stack. */
function family(rec: DecisionRecord): string[] {
  switch (rec.kind) {
    case "correct":
    case "keep": {
      const a = rec.anchor;
      return [JSON.stringify(["text", rec.pdf, a.page, ...a.bbox.map(roundHalfUp), a.text_sha, rec.range.before, rec.range.was, rec.range.after])];
    }
    case "alt": return [JSON.stringify(["alt", rec.pdf, rec.anchor.page, rec.anchor.crop_sha])];
    case "dismiss": return [...new Set(rec.findings)].map((f) => JSON.stringify(["dismiss", rec.pdf, f]));
    case "proposal": return [JSON.stringify(["proposal", rec.pdf, rec.proposal])];
    case "font-map": return [JSON.stringify(["font-map", rec.pdf, rec.map])];
  }
}

/** What a `supersedes` of this record may name. */
function kin(rec: DecisionRecord): readonly DecisionRecord["kind"][] {
  if (rec.kind === "correct" || rec.kind === "keep") return ["correct", "keep"];
  if (rec.kind === "alt") return ["alt"];
  return [];
}

const isDecision = (p: Parsed | undefined): p is { type: "record"; record: DecisionRecord } =>
  p?.type === "record" && p.record.kind !== "undo" && p.record.kind !== "reattach";

/** A pure function of the records in store order. The first record with an id wins. A record sits
 *  on the stack of its family and replaces the one beneath it; `supersedes` also puts it on the
 *  stack of an earlier record of its kind. `undo` takes a record off its stacks. A record is
 *  active when it is on top of every stack it sits on (a dismissal: of any, each fingerprint being
 *  its own family). */
export function fold(snapshot: Snapshot): Fold {
  const seen = new Map<string, Parsed>();
  const stacks = new Map<string, DecisionRecord[]>();
  const keys = new Map<string, string[]>();
  const order: DecisionRecord[] = [];
  const undone = new Set<string>();
  const invalid: InvalidRecord[] = [];
  const unsupported: UnsupportedRecord[] = [];
  const reattach: Reattach[] = [];
  let duplicate = 0;

  for (const { parsed } of snapshot.entries) {
    const ident = parsed.type === "record" ? parsed.record.id : parsed.id;
    if (ident !== null && seen.has(ident)) {
      duplicate++;
      continue;
    }
    if (ident !== null) seen.set(ident, parsed);
    if (parsed.type === "unsupported") {
      unsupported.push({ id: parsed.id, v: parsed.v, kind: parsed.kind });
      continue;
    }
    if (parsed.type === "invalid") {
      invalid.push({ id: parsed.id, reason: parsed.reason });
      continue;
    }
    const rec = parsed.record;
    if (rec.kind === "reattach") {
      reattach.push(rec);
    } else if (rec.kind === "undo") {
      const target = seen.get(rec.of);
      if (!isDecision(target)) {
        invalid.push({ id: rec.id, reason: "undo names no earlier decision" });
      } else if (!undone.has(target.record.id)) {
        undone.add(target.record.id);
        for (const key of keys.get(target.record.id) ?? []) {
          const stack = stacks.get(key) ?? [];
          const at = stack.indexOf(target.record);
          if (at >= 0) stack.splice(at, 1);
        }
      }
    } else {
      let own = family(rec);
      if ("supersedes" in rec && rec.supersedes !== null) {
        const target = seen.get(rec.supersedes);
        if (isDecision(target) && kin(rec).includes(target.record.kind) && keys.has(target.record.id)) {
          const first = (keys.get(target.record.id) ?? []).slice(0, 1).filter((k) => !own.includes(k));
          own = [...own, ...first];
        } else {
          invalid.push({ id: rec.id, reason: "supersedes names no earlier record of its kind" });
        }
      }
      keys.set(rec.id, own);
      order.push(rec);
      for (const key of own) {
        const stack = stacks.get(key);
        if (stack) stack.push(rec);
        else stacks.set(key, [rec]);
      }
    }
  }

  const top = (key: string, rec: DecisionRecord): boolean => stacks.get(key)?.at(-1) === rec;
  const active = order.filter((rec) => {
    if (undone.has(rec.id)) return false;
    const own = keys.get(rec.id) ?? [];
    return rec.kind === "dismiss" ? own.some((k) => top(k, rec)) : own.every((k) => top(k, rec));
  });
  return {
    active, reattach, records: snapshot.entries.length, torn: snapshot.torn, unreadable: snapshot.unreadable.length,
    duplicate, invalid, unsupported, error: snapshot.error, store: snapshot.store, undone: [...undone],
  };
}

/** The entries of `snapshot` a build whose ledger names `store` and `bytes` has not read: all of
 *  them when the store is another, else those whose line starts at or after `bytes`. */
export function pending(snapshot: Snapshot, ledger: { store: string | null; bytes: number }) {
  if (snapshot.store !== ledger.store) return snapshot.entries;
  return snapshot.entries.filter((e) => e.offset >= ledger.bytes);
}
