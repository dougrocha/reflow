"""One bounded, failure-safe tag read per conversion, shared by evidence and order."""

from types import SimpleNamespace

import reflow.extract.tags as tags
from reflow.analyze import order_evidence
from reflow.model import Document, Node, PageInfo, Role
from reflow.run import RunState


def fake_run(tmp_path, pages=1):
    doc = Document(root=Node(Role.DOC), pages=[PageInfo(i + 1, 100, 100) for i in range(pages)])
    return SimpleNamespace(doc=doc, working=tmp_path / "w.pdf", state=RunState(),
                           workers=SimpleNamespace(jobs=1, pool=None),
                           options=SimpleNamespace(stage_cache=None))


def test_a_raising_reader_does_not_abort(tmp_path, monkeypatch):
    monkeypatch.setattr(tags, "read_tags", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    run = fake_run(tmp_path)
    assert tags.shared_tags(run) == {}
    assert [f.code for f in run.doc.root.facts] == ["order-unchecked"]
    assert order_evidence.apply(run) == []
    assert len(run.doc.root.facts) == 1          # not read, not recorded, twice


def test_an_over_cap_document_is_skipped(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tags, "read_tags", lambda *a, **k: calls.append(1) or {})
    run = fake_run(tmp_path, pages=tags.MAX_TAG_PAGES + 1)
    assert tags.shared_tags(run) == {} and order_evidence.apply(run) == []
    assert calls == []


def test_the_tags_are_read_once_for_both_passes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tags, "read_tags", lambda *a, **k: calls.append(1) or {})
    run = fake_run(tmp_path)
    tags.shared_tags(run)
    order_evidence.apply(run)
    tags.shared_tags(run)
    assert len(calls) == 1
