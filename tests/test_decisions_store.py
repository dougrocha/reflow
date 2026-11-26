"""The decision store: its snapshot, and the fold that picks the active decisions.

The `tests/fixtures/decisions/<case>.jsonl` files and their `.expected.json` are shared with the
TypeScript fold of the web app, so the expected JSON is a plain rendering of the fold.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from decisions_kit import append, mint_id, new_store
from reflow.analyze import decisions as d

FIXTURES = Path(__file__).parent / "fixtures" / "decisions"
CASES = sorted(p.name.removesuffix(".jsonl") for p in FIXTURES.glob("*.jsonl"))
PDF = "a1" * 32
OTHER = "b2" * 32


def render(snapshot: d.Snapshot, fold: d.Fold) -> dict:
    """The language-neutral rendering of a fold that the fixtures' expected files hold."""
    return {"store": snapshot.store, "bytes": snapshot.bytes, "lines": snapshot.lines,
            "counts": {"records": fold.records, "torn": fold.torn,
                       "unreadable": fold.unreadable, "duplicate": fold.duplicate},
            "active": [r.id for r in fold.active], "invalid": [i.id for i in fold.invalid],
            "unsupported": [{"id": u.id, "v": u.v, "kind": u.kind} for u in fold.unsupported],
            "reattach": [{"from": r.from_, "to": r.to} for r in fold.reattach],
            "error": fold.error}


@pytest.mark.parametrize("case", CASES)
def test_the_fold_of_a_shared_fixture_is_its_expected_file(case):
    snapshot = d.read(FIXTURES / f"{case}.jsonl")
    expected = json.loads((FIXTURES / f"{case}.expected.json").read_text(encoding="utf-8"))
    assert render(snapshot, d.fold(snapshot)) == expected


def test_there_is_a_fixture_for_every_store_case():
    assert {"torn-tail", "torn-then-append", "no-header", "duplicate-id", "undo-restores-older",
            "undo-invalid", "supersedes-across-keys", "keep-replaces-correct", "reattach-one-hop",
            "unsupported", "proposal-family", "font-map-family"} <= set(CASES)


# -- the kit's records ---------------------------------------------------------------------------


def anchor(**kw) -> dict:
    return {"page": 1, "bbox": [72.0, 100.0, 300.0, 112.0], "text_sha": "0123456789abcdef",
            "crop_sha": "", "hint": "n-1", **kw}


def correct(now="80110", **kw) -> dict:
    return {"v": 1, "id": mint_id(), "at": "2026-10-28T09:14:03Z", "pdf": PDF, "kind": "correct",
            "target": {"anchor": anchor(), "range": {"was": "8011O", "before": "CO ",
                                                      "after": "", "rule": "zip-digit"}},
            "now": now, **kw}


def undo(of: str) -> dict:
    return {"v": 1, "id": mint_id(), "at": "2026-10-28T09:14:03Z", "pdf": PDF, "kind": "undo",
            "of": of}


@pytest.fixture
def store(tmp_path) -> Path:
    path = tmp_path / "demo.jsonl"
    new_store(path, "demo")
    return path


# -- the read protocol -----------------------------------------------------------------------------


def test_a_store_with_only_a_header_is_empty(store):
    snap = d.read(store)
    assert snap.error is None and snap.entries == () and snap.lines == 1
    assert snap.store is not None and len(snap.store) == 16
    assert snap.bytes == store.stat().st_size


def test_a_missing_file_is_an_empty_store(tmp_path):
    snap = d.read(tmp_path / "none.jsonl")
    assert (snap.store, snap.bytes, snap.lines, snap.error, snap.entries) == (None, 0, 0, None, ())
    assert d.fold(snap).active == ()


def test_a_path_that_is_not_a_file_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="not a file"):
        d.read(tmp_path)


def test_a_torn_tail_is_counted_and_not_read(store):
    with open(store, "ab") as f:
        f.write(b'{"v":1,"id":"x')
    snap = d.read(store)
    assert snap.torn == 1 and snap.entries == () and snap.lines == 1


def test_a_torn_line_then_a_valid_append_gives_one_unreadable_and_the_record(store):
    with open(store, "ab") as f:
        f.write(b'{"v":1,"id":"x')
    record = correct()
    append(store, record)
    snap = d.read(store)
    assert snap.torn == 0 and len(snap.unreadable) == 1
    assert [e.record.id for e in snap.entries] == [record["id"]]
    assert d.fold(snap).unreadable == 1


def test_unreadable_lines_keep_their_byte_offsets(store):
    header_end = store.stat().st_size
    with open(store, "ab") as f:
        f.write(b"nope\n")
        f.write(b"[1]\n")
    snap = d.read(store)
    assert snap.unreadable == (header_end, header_end + 5)


def test_a_file_whose_first_line_is_not_a_header_has_a_no_header_error(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(correct()) + "\n", encoding="utf-8")
    snap = d.read(path)
    assert snap.error == "no-header" and snap.entries == () and snap.store is None
    assert d.fold(snap).active == () and d.fold(snap).error == "no-header"


def test_bytes_stops_at_the_last_newline(store):
    append(store, correct())
    complete = store.stat().st_size
    with open(store, "ab") as f:
        f.write(b'{"torn')
    snap = d.read(store)
    assert snap.bytes == complete and snap.torn == 1


def test_a_record_appended_after_a_read_lies_past_its_bytes(store):
    first = correct()
    append(store, first)
    snap = d.read(store)
    later = correct(now="80119")
    append(store, later)
    newer = d.read(store)
    pending = d.pending(newer, store=snap.store, bytes_=snap.bytes)
    assert [e.record.id for e in pending] == [later["id"]]
    assert all(e.offset < snap.bytes for e in snap.entries)


def test_every_record_is_pending_for_a_build_of_another_store(store):
    append(store, correct())
    snap = d.read(store)
    assert len(d.pending(snap, store="0" * 16, bytes_=10**9)) == 1


def test_each_record_keeps_the_offset_where_its_line_starts(store):
    start = store.stat().st_size
    a, b = correct(), correct(now="80119")
    append(store, a)
    mid = store.stat().st_size
    append(store, b)
    assert [e.offset for e in d.read(store).entries] == [start, mid]


def test_a_line_over_8_kb_is_unreadable(store):
    with open(store, "ab") as f:
        f.write(b'{"v":1,"pad":"' + b"x" * 9000 + b'"}\n')
    assert len(d.read(store).unreadable) == 1


def worker(path: str, n: int) -> None:
    for i in range(n):
        append(Path(path), correct(now=f"{i}"))


def test_two_processes_appending_fifty_records_each_give_a_hundred_parseable_lines(store):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=worker, args=(str(store), 50)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0
    snap = d.read(store)
    assert len(snap.entries) == 100 and snap.unreadable == () and snap.torn == 0
    assert all(isinstance(e.record, d.Correct) for e in snap.entries)
    assert len(d.fold(snap).active) == 1   # one family: the last write wins


# -- parsing at the boundary ---------------------------------------------------------------------


def test_a_correct_record_parses_to_a_typed_record():
    rec = d.parse_record(correct())
    assert isinstance(rec, d.Correct)
    assert rec.anchor.page == 1 and rec.anchor.bbox == (72.0, 100.0, 300.0, 112.0)
    assert rec.range.was == "8011O" and rec.now == "80110" and rec.kind == d.Kind.CORRECT


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("now"), lambda r: r.update(pdf="short"), lambda r: r.update(id="bad"),
    lambda r: r["target"]["anchor"].update(page=0), lambda r: r["target"]["anchor"].update(page=True),
    lambda r: r["target"]["anchor"].update(bbox=[1, 2, 3]), lambda r: r["target"].pop("range"),
    lambda r: r["target"]["range"].update(was=""), lambda r: r.update(now=5),
    lambda r: r["target"]["anchor"].update(text_sha="xyz"), lambda r: r.update(supersedes=3)])
def test_a_known_kind_with_a_wrong_field_is_invalid(mutate):
    rec = correct()
    mutate(rec)
    assert isinstance(d.parse_record(rec), d.Invalid)


def test_an_unknown_version_or_kind_is_unsupported_not_invalid():
    got = d.parse_record({**correct(), "v": 2})
    assert isinstance(got, d.Unsupported) and got.v == 2
    got = d.parse_record({**correct(), "kind": "mystery"})
    assert isinstance(got, d.Unsupported) and got.kind == "mystery"


def test_a_record_is_frozen():
    rec = d.parse_record(correct())
    with pytest.raises(Exception):
        rec.now = "x"   # type: ignore[misc]


def test_a_proposal_verdict_needs_a_digest_and_a_proposal_id():
    base = {"v": 1, "id": mint_id(), "at": "t", "pdf": PDF, "kind": "proposal",
            "proposal": "pv-1", "verdict": "accept", "text": "d" * 64}
    assert isinstance(d.parse_record(base), d.ProposalVerdict)
    assert isinstance(d.parse_record({**base, "verdict": "maybe"}), d.Invalid)
    assert isinstance(d.parse_record({**base, "text": "d"}), d.Invalid)
    assert isinstance(d.parse_record({**base, "proposal": "x"}), d.Invalid)


# -- the limits on `now` ---------------------------------------------------------------------------


@pytest.mark.parametrize("now,was,ok", [
    ("80110", "8011O", True), ("", "8011O", False), ("a\nb", "ab", False), ("a\tb", "ab", False),
    ("a‮b", "ab", False), ("a⁦b", "ab", False), ("a‏b", "ab", False),
    ("a b", "ab", False), ("x" * 201, "x" * 100, False), ("x" * 200, "x" * 50, True),
    ("x" * 9, "ab", False), ("x" * 8, "ab", True), ("café", "cafe", True)])
def test_now_must_be_one_clean_line_within_limits(now, was, ok):
    assert (d.invalid_now(now, was) is None) is ok


# -- the fold --------------------------------------------------------------------------------------


def folded(store: Path) -> d.Fold:
    return d.fold(d.read(store))


def test_the_first_record_with_an_id_wins(store):
    first = correct()
    append(store, first)
    append(store, {**correct(now="80119"), "id": first["id"]})
    fold = folded(store)
    assert [r.now for r in fold.active] == ["80110"] and fold.duplicate == 1


def test_undoing_the_newest_decision_restores_the_older_one(store):
    a, b = correct(), correct(now="80119")
    append(store, a)
    append(store, b)
    assert [r.id for r in folded(store).active] == [b["id"]]
    append(store, undo(b["id"]))
    assert [r.id for r in folded(store).active] == [a["id"]]


def test_a_second_undo_of_the_same_record_does_nothing(store):
    a = correct()
    append(store, a)
    append(store, undo(a["id"]))
    append(store, undo(a["id"]))
    fold = folded(store)
    assert fold.active == () and fold.invalid == ()


def test_undo_of_an_undo_an_unknown_id_and_a_later_record_are_invalid(store):
    a = correct()
    u = undo(a["id"])
    append(store, a)
    append(store, u)
    bad = [undo(u["id"]), undo("zzzz-00000000"), undo("m2k9x4q6-00000006")]
    for record in bad:
        append(store, record)
    later = {**correct(now="80119"), "id": "m2k9x4q6-00000006"}
    append(store, later)
    fold = folded(store)
    assert [i.id for i in fold.invalid] == [r["id"] for r in bad]
    assert [r.id for r in fold.active] == [later["id"]]


def test_keep_after_correct_of_the_same_range_replaces_it(store):
    a = correct()
    k = {**correct(), "kind": "keep"}
    del k["now"]
    append(store, a)
    append(store, k)
    [active] = folded(store).active
    assert isinstance(active, d.Keep) and active.id == k["id"]


def test_a_proposal_replaces_a_proposal_of_the_same_id_and_undo_restores(store):
    def prop(pid, verdict):
        return {"v": 1, "id": mint_id(), "at": "t", "pdf": PDF, "kind": "proposal",
                "proposal": pid, "verdict": verdict, "text": "d" * 64}
    a, b, other = prop("pv-a", "accept"), prop("pv-a", "reject"), prop("pv-b", "accept")
    for r in (a, b, other):
        append(store, r)
    assert [r.id for r in folded(store).active] == [b["id"], other["id"]]
    append(store, undo(b["id"]))
    assert [r.id for r in folded(store).active] == [a["id"], other["id"]]


def test_supersedes_replaces_across_family_keys_and_undo_brings_it_back(store):
    a = correct()
    b = correct(now="80119", supersedes=a["id"])
    b["target"]["anchor"]["text_sha"] = "9999999999999999"   # the text changed once applied
    append(store, a)
    append(store, b)
    assert [r.id for r in folded(store).active] == [b["id"]]
    append(store, undo(b["id"]))
    assert [r.id for r in folded(store).active] == [a["id"]]


def test_a_bad_supersedes_is_invalid_and_the_record_stands_alone(store):
    a = correct()
    b = correct(supersedes="m2k9x4q0-00000000")
    b["target"]["anchor"]["text_sha"] = "9999999999999999"
    append(store, a)
    append(store, b)
    fold = folded(store)
    assert [i.id for i in fold.invalid] == [b["id"]]
    assert [r.id for r in fold.active] == [a["id"], b["id"]]


def test_reattachments_do_not_chain():
    fold_ = d.Fold(active=(), reattach=(d.Reattach(id="m-00000001", at="t", pdf=PDF, from_=PDF, to=OTHER),
                                        d.Reattach(id="m-00000002", at="t", pdf=PDF, from_=OTHER, to="c3" * 32)),
                   records=2, torn=0, unreadable=0, duplicate=0, invalid=(), unsupported=(),
                   error=None, store=None)
    assert d.eligible_pdfs(fold_, OTHER) == {OTHER, PDF}
    assert d.eligible_pdfs(fold_, "c3" * 32) == {"c3" * 32, OTHER}
    assert d.eligible_pdfs(fold_, PDF) == {PDF}


@pytest.mark.parametrize("kind", [[], {}, 1, None])
def test_a_kind_that_is_not_a_string_is_unsupported(kind):
    assert isinstance(d.parse_record({"v": 1, "id": "m1-00000001", "kind": kind}), d.Unsupported)


@pytest.mark.parametrize("verdict", [[], {}, 1])
def test_a_proposal_verdict_that_is_not_a_string_is_invalid(verdict):
    parsed = d.parse_record({"v": 1, "id": "m1-00000001", "at": "t", "pdf": "a1" * 32,
                             "kind": "proposal", "proposal": "pv-1", "verdict": verdict,
                             "text": "c3" * 32})
    assert isinstance(parsed, d.Invalid)
