import io
import json

import pikepdf
import pytest
from PIL import Image
from typer.testing import CliRunner

from conftest import make_pdf
from reflow.analyze import assist, enrich
from reflow.analyze.assist import Region, Transcription, draft, parse_transcription, regions
from reflow.analyze.enrich import AltResult
from reflow.analyze.vision import (OllamaVision, ProviderError, ProviderUnavailable, Reply,
                                   VisionImage)
from reflow.analyze.review import queue
from reflow.cli import app
from reflow.convert import convert
from reflow.ingest.pages import ingest
from reflow.model import Document, Fact, Node, PageInfo, Role, Source
from reflow.providers import TRANSCRIPTION_MODEL
from reflow.render.site import render_html
from test_ocr import FakeRunner
from testkit import resolving


class FakeModel:
    name = "local"
    cache_id = "fake:1"
    confidence = 0.5
    workers = 1

    def __init__(self, result=Transcription("239.0 g CuSO₄\n= 1.497 mol\n\n[illegible]", True)):
        self.result = result
        self.calls = []
        self.told = []
        self.alt_calls = []

    def transcribe(self, image, media_type, told=""):
        self.calls.append((image, media_type))
        self.told.append(told)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def describe(self, image, media_type, context):
        self.alt_calls.append(context)
        return AltResult("photo", False, "A tree beside a house.", "", False)

    def close(self):
        pass


def png(label: str = "image", dark: int = 2000) -> bytes:
    """A crop with `dark` pixels of ink; `label` makes its bytes its own (in pixels too light to
    be ink)."""
    image = Image.new("L", (100, 100), 255)
    image.putdata([0] * dark + [255] * (10_000 - dark))
    for i, c in enumerate(label.encode()):
        image.putpixel((i, 99), 200 + c % 56)
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def document():
    return Document(Node(Role.DOC, children=[
        Node(Role.P, page=1, text="OCR baseline"),
        Node(Role.P, page=2, text="Next page")]),
        pages=[PageInfo(1, 612, 792), PageInfo(2, 612, 792)], meta={"lang": "en"})


def test_drafts_keep_baseline_page_bbox_metadata_and_review(tmp_path):
    doc = document()
    region = Region(1, (10, 20, 400, 300), "ocr-empty")
    draft(doc, [(region, png("image"))], FakeModel(), tmp_path / "cache.json")
    drafts = [n for n in doc.root.children if n.source == Source.LLM]
    assert [n.text for n in doc.root.children] == [
        "OCR baseline", "239.0 g CuSO₄\n= 1.497 mol", "[illegible]", "Next page"]
    for node in drafts:
        assert node.page == 1 and node.bbox == (10, 20, 400, 300)
        assert node.attrs["handwritten"] is True
        assert node.attrs["provenance"] == "ai-drafted" and node.attrs["reviewed"] is False
    review = {i["id"]: i for i in queue(doc)}
    for node in drafts:
        assert review[node.id]["label"] == "AI-drafted, not reviewed"
        assert review[node.id]["bbox"] == [10, 20, 400, 300]
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text()
    assert html.count('data-provenance="ai-drafted"') == 2
    assert 'data-handwritten="true"' in html and 'data-reviewed="false"' in html
    assert "Handwritten · AI-drafted, not reviewed" in html
    assert "239.0 g CuSO₄<br>= 1.497 mol" in html
    assert Document.from_json(doc.to_json()).to_dict() == doc.to_dict()


def test_cache_and_idempotence_use_image_model_and_prompt(tmp_path, monkeypatch):
    doc = document()
    model = FakeModel()
    region = Region(1, (0, 0, 612, 792), "ocr-empty")
    candidates = [(region, png("image"))]
    cache = tmp_path / "cache.json"
    draft(doc, candidates, model, cache)
    draft(doc, candidates, model, cache)
    assert len(model.calls) == 1 and len(doc.root.children) == 4
    stats = draft(document(), candidates, model, cache)
    assert stats == {"drafted": 0, "cached": 1, "rejected": 0, "failed": 0}
    draft(document(), [(region, png("changed"))], model, cache)
    model.cache_id = "fake:2"
    draft(document(), candidates, model, cache)
    monkeypatch.setattr(assist, "PROMPT_VERSION", assist.PROMPT_VERSION + 1)
    draft(document(), candidates, model, cache)
    assert len(model.calls) == 4


def test_failures_preserve_ocr_and_are_reviewed_without_caching(tmp_path):
    doc = document()
    cache = tmp_path / "cache.json"
    stats = draft(doc, [(Region(1, (0, 0, 10, 10), "ocr-empty"), png("image"))],
                  FakeModel(ProviderError("out of memory")), cache)
    assert stats["failed"] == 1
    assert [n.text for n in doc.root.children] == ["OCR baseline", "Next page"]
    assert json.loads(cache.read_text()) == {}
    assert any(i["label"] == "AI transcription failed" and "out of memory" in i["text"]
               for i in queue(doc))


@pytest.mark.parametrize("bad", ["bad JSON", '{"text": 4, "handwritten": true}',
                                '{"text": "x", "handwritten": "false"}'])
def test_rejects_malformed_model_output(bad):
    with pytest.raises(ProviderError):
        parse_transcription(bad)


def test_empty_transcription_is_cached_without_inventing_content(tmp_path):
    doc, model = document(), FakeModel(Transcription("", False))
    candidates = [(Region(1, (0, 0, 10, 10), "ocr-empty"), png("image"))]
    cache = tmp_path / "cache.json"
    draft(doc, candidates, model, cache)
    draft(document(), candidates, model, cache)
    assert len(model.calls) == 1 and len(doc.root.children) == 2


def test_identical_crops_share_cache_but_keep_both_source_locations(tmp_path):
    doc, model = document(), FakeModel(Transcription("Signed", True))
    draft(doc, [(Region(1, (0, 0, 10, 10), "ocr-empty"), png("same")),
                (Region(1, (20, 20, 30, 30), "ocr-empty"), png("same"))],
          model, tmp_path / "cache.json")
    drafts = [n for n in doc.root.children if n.source == Source.LLM]
    assert len(model.calls) == 1
    assert [n.bbox for n in drafts] == [(0, 0, 10, 10), (20, 20, 30, 30)]
    assert len({n.id for n in drafts}) == 2


def test_plain_scans_use_ocr_text_first_and_handwriting_flags_crop(tmp_path):
    pdf = make_pdf(tmp_path / "pages.pdf", ["text", "scan", "empty"])
    doc = ingest(pdf)
    assert [(r.page, r.reason) for r, _ in regions(doc, pdf)] == [(2, "ocr-empty")]
    node = Node(Role.P, page=1, bbox=(100, 200, 200, 250), text="OCR flags handwriting",
                attrs={"handwritten": True})
    doc.root.children.append(node)
    selected = regions(doc, pdf)
    assert selected[0][0] == Region(1, (100, 200, 200, 250), "ocr-handwriting", True)
    assert Image.open(io.BytesIO(selected[0][1])).size == (200, 100)


def test_low_letter_share_region_triggers_even_when_page_average_is_good(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan+text"])
    with pikepdf.open(pdf, allow_overwriting_input=True) as source:
        page = source.pages[0]
        extra = (b"BT /F1 12 Tf 72 650 Td "
                 b"(This printed paragraph contains plenty of correctly recognized words.) Tj ET\n"
                 b"BT /F1 12 Tf 72 550 Td (ATf = 1.34m x 1.86 x 3 = 7.48 C) Tj ET\n")
        page.Contents = source.make_stream(page.Contents.read_bytes() + extra)
        source.save(pdf)
    candidates = regions(ingest(pdf), pdf)
    assert candidates[0][0].reason == "ocr-low-letter-share-region"


def test_page_handwriting_fact_is_preserved_even_when_model_says_false(tmp_path):
    pdf = make_pdf(tmp_path / "text.pdf", ["text"])
    doc = ingest(pdf)
    doc.pages[0].facts.append(Fact("ocr-page", Source.OCR, data={"handwritten": True}))
    candidates = regions(doc, pdf)
    assert candidates[0][0].handwritten is True
    draft(doc, candidates, FakeModel(Transcription("A handwritten note", False)),
          tmp_path / "cache.json")
    assert doc.root.children[0].attrs["handwritten"] is True
    assert queue(doc)[0]["label"] == "AI-drafted, not reviewed"


def _unread_figure(page, box, handwritten):
    return Node(Role.FIGURE, page=page, bbox=box,
                attrs={"unread": True, "handwritten": handwritten,
                       "kind": "handwriting" if handwritten else "unread-text"})


def test_unread_figures_are_targets_of_their_own(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan+text"])
    doc = ingest(pdf)
    assert regions(doc, pdf) == []
    grey = _unread_figure(1, (100.0, 200.0, 300.0, 260.0), False)
    pen = _unread_figure(1, (100.0, 400.0, 300.0, 460.0), True)
    doc.root.children += [grey, pen]
    assert [r for r, _ in regions(doc, pdf)] == [
        Region(1, grey.bbox, "unread-figure", False, grey.id),
        Region(1, pen.bbox, "unread-figure", True, pen.id)]


def test_a_grainy_scan_is_sent_whole(tmp_path):
    # Its page scan looks for no writing (scan-noisy), so the model sees the page.
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan+text"])
    doc = ingest(pdf)
    doc.pages[0].facts.append(Fact("scan-noisy", Source.HEURISTIC, ""))
    assert [(r.reason, r.bbox) for r, _ in regions(doc, pdf)] == [
        ("scan-noisy", (0.0, 0.0, 612.0, 792.0))]


def test_each_draft_sits_right_under_its_own_crop(tmp_path):
    first = _unread_figure(1, (10.0, 20.0, 200.0, 60.0), True)
    second = _unread_figure(1, (10.0, 300.0, 200.0, 340.0), False)
    doc = document()
    doc.root.children[1:1] = [first, Node(Role.P, page=1, text="Between"), second]
    draft(doc, [(Region(1, first.bbox, "unread-figure", True, first.id), png("one")),
                (Region(1, second.bbox, "unread-figure", False, second.id), png("two"))],
          FakeModel(Transcription("Sara Smith", False)), tmp_path / "cache.json")
    texts = [n.text if n.role != Role.FIGURE else n.id for n in doc.root.children]
    assert texts == ["OCR baseline", first.id, "Sara Smith", "Between", second.id, "Sara Smith",
                     "Next page"]
    drafts = [n for n in doc.root.children if n.source == Source.LLM]
    assert [d.attrs["assist_for"] for d in drafts] == [first.id, second.id]
    assert [d.attrs["handwritten"] for d in drafts] == [True, False]
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    assert "Handwritten · AI-drafted, not reviewed" in (tmp_path / "index.html").read_text()


def test_regions_are_not_read_when_assist_is_off(tmp_path, monkeypatch):
    def refuse(*args):
        raise AssertionError("regions runs only for --assist")

    monkeypatch.setattr(assist, "regions", refuse)
    convert(make_pdf(tmp_path / "scan.pdf", ["scan+text"]), tmp_path / "out", page_images=False)


def test_existing_alt_and_decorative_figures_are_never_sent_to_model(tmp_path):
    missing = Node(Role.FIGURE, page=1, attrs={"needs": "alt"})
    tagged = Node(Role.FIGURE, page=1, attrs={"alt": "Human description", "needs": "alt"})
    decor = Node(Role.FIGURE, page=1, attrs={"decorative": True, "needs": "alt"})
    fact_only = Node(Role.FIGURE, page=1)
    fact_only.fact("alt-missing", Source.HEURISTIC)
    unflagged = Node(Role.FIGURE, page=1)
    doc = Document(Node(Role.DOC, children=[missing, tagged, decor, fact_only, unflagged]),
                   pages=[PageInfo(1, 612, 792)], meta={"lang": "en"})
    doc.ensure_unique_ids()
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    model = FakeModel()
    stats = enrich.enrich_figures(doc, {n.id: image for n in doc.root.children}, model)
    # Identical images with identical context share one model answer.
    assert len(model.alt_calls) == 1 and stats["described"] + stats["cached"] == 2
    assert tagged.attrs["alt"] == "Human description"
    assert "provenance" not in decor.attrs and "provenance" not in unflagged.attrs
    assert all(i["severity"] == "warning" for i in queue(doc) if i["id"] == fact_only.id)
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    assert (tmp_path / "index.html").read_text().count('data-provenance="ai-drafted"') == 2


def test_convert_off_by_default_then_ocr_before_assist_and_cache_survives_staging(tmp_path, monkeypatch):
    model = FakeModel()
    names = []

    def factory(name, **kw):
        names.append(name)
        return model if name == "ollama" else enrich.NoneProvider()

    monkeypatch.setattr("reflow.pipeline.make_provider", factory)
    runner = FakeRunner("1234 #%&")
    monkeypatch.setattr("reflow.prepare.make_runner", lambda _: runner)
    transcribe = model.transcribe

    def after_ocr(image, media_type, told=""):
        assert runner.calls and runner.calls[-1][0] == [1]
        return transcribe(image, media_type, told)

    monkeypatch.setattr(model, "transcribe", after_ocr)
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan"])
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    assert names == ["none"] and model.calls == []
    convert(pdf, out, assist="local", page_images=False)
    doc = Document.from_json((out / "document.json").read_text())
    assert doc.meta["ai_assist"] == {"provider": "local", "drafted": 1, "cached": 0, "rejected": 0,
                                     "failed": 0}
    assert doc.pages[0].facts[0].code == "ocr-page"
    assert any("1234" in (n.text or "") for n in doc.root.walk())
    assert len(model.calls) == 1
    convert(pdf, out, assist="local", page_images=False)
    assert len(model.calls) == 1
    doc = Document.from_json((out / "document.json").read_text())
    assert doc.meta["ai_assist"]["cached"] == 1
    assert any(i["label"] == "AI-drafted, not reviewed"
               for i in json.loads((out / "review.json").read_text()))


def test_invalid_assist_rejected_before_output(tmp_path):
    with pytest.raises(ValueError, match="unknown assist"):
        convert(tmp_path / "missing.pdf", tmp_path / "out", assist="cloud")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("command", ["convert", "convert-many"])
def test_cli_assist_uses_fake_model_and_reports_drafts(tmp_path, monkeypatch, command):
    model = FakeModel()
    monkeypatch.setattr("reflow.pipeline.make_provider", lambda name, **kw: model)
    monkeypatch.setattr("reflow.prepare.make_runner", lambda _: FakeRunner("1234 #%&"))
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan"])
    out = tmp_path / "out"
    args = [command, str(pdf)]
    args += ["-o", str(out)] if command == "convert" else [str(out)]
    result = CliRunner().invoke(app, [*args, "--assist", "local", "--jobs", "1",
                                     "--no-page-images"])
    assert result.exit_code == 0, result.output
    doc = Document.from_json((out / "document.json").read_text())
    assert doc.meta["ai_assist"]["drafted"] == 1 and len(model.calls) == 1
    if command == "convert":
        assert "AI transcription (local): 1 drafted" in " ".join(result.output.split())


def test_token_limit_is_a_reviewable_failure_without_gpu(tmp_path, monkeypatch):
    class Cut:
        cache_id = "fake"

        def ask(self, *args, **kw):
            return Reply('{"text":"cut off', True)

    provider = enrich.ModelProvider("local", Cut())
    doc = document()
    stats = draft(doc, [(Region(1, (0, 0, 10, 10), "ocr-empty"), png("image"))],
                  provider, tmp_path / "cache.json")
    assert stats["rejected"] == 1   # a cut-off reading, read twice
    assert any("cut-off" in i["text"] and "token limit" in i["text"] for i in queue(doc))


def no_gpu(monkeypatch):
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    mps = getattr(torch.backends, "mps", None)
    if mps is not None:
        monkeypatch.setattr(mps, "is_available", lambda: False)


def test_auto_device_resolves_to_cpu_and_named_gpu_fails_without_one(tmp_path, monkeypatch):
    from reflow.device import resolve_device
    from reflow.run import Options, with_device
    no_gpu(monkeypatch)
    assert [resolve_device(d) for d in ("auto", "cpu")] == ["cpu"] * 2
    for named in ("cuda", "mps"):
        with pytest.raises(ValueError, match=f"{named.upper()} requested but not available"):
            resolve_device(named)
    with pytest.raises(ValueError, match="use --device auto or --device cpu"):
        convert(tmp_path / "missing.pdf", tmp_path / "out", engine="hybrid", device="cuda")
    assert not (tmp_path / "out").exists()
    opts = Options(engine="hybrid")
    assert (opts.device_requested, opts.device) == ("auto", "auto")   # a value: nothing probed
    resolved = with_device(opts)
    assert (resolved.device_requested, resolved.device) == ("auto", "cpu")
    assert with_device(Options()).device == "auto"      # no model: torch is never consulted


def test_local_alt_text_runs_without_cuda_and_assist_asks_ollama(tmp_path, monkeypatch):
    no_gpu(monkeypatch)
    seen = []
    real = enrich.make_provider

    def make(name, **kw):
        provider = real(name, **kw)
        seen.append(getattr(provider.client, "device", None) or provider.client.model)
        provider.client.ask = lambda *a, **k: Reply(
            '{"text": "1234", "handwritten": true, "kind": "photo", "decorative": false,'
            ' "alt": "A tree.", "long_description": ""}', False)
        return provider

    monkeypatch.setattr("reflow.pipeline.make_provider", make)
    monkeypatch.setattr("reflow.prepare.make_runner", lambda _: FakeRunner("1234 #%&"))
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan"])
    summary = convert(pdf, tmp_path / "out", assist="local", alt_text="local", page_images=False)
    # Transcription runs through Ollama; only alt text loads a model in this process.
    assert seen == [TRANSCRIPTION_MODEL, "cpu"]
    assert summary.device == "cpu" and summary.assist["drafted"] == 1
    assert summary.device_notes
    provider = enrich.make_provider("local")
    assert provider.client.device == "cpu"
    provider.client.ask = lambda *a, **k: Reply(
        '{"kind": "photo", "decorative": false, "alt": "A tree.", "long_description": ""}', False)
    assert provider.describe(b"x", "image/png", enrich.FigureContext(1, "", "", "")).alt == "A tree."


def test_cpu_out_of_memory_leaves_the_figure_for_review_and_other_errors_propagate(tmp_path, monkeypatch):
    no_gpu(monkeypatch)
    provider = enrich.make_provider("local", device="cpu").client
    picture = VisionImage(_png(), "image/png")
    monkeypatch.setattr(provider, "load", lambda: (_Model(RuntimeError(
        "[enforce fail] DefaultCPUAllocator: can't allocate memory: you tried to allocate 9 bytes")),
        _Processor()))
    with pytest.raises(ProviderError, match="out of memory on cpu"):
        provider.ask("s", "p", picture, None, 8)
    monkeypatch.setattr(provider, "load", lambda: (_Model(RuntimeError("MPS backend out of memory")),
                                                    _Processor()))
    with pytest.raises(ProviderError):
        provider.ask("s", "p", picture, None, 8)
    monkeypatch.setattr(provider, "load", lambda: (_Model(RuntimeError("shape mismatch")),
                                                    _Processor()))
    with pytest.raises(RuntimeError, match="shape mismatch"):
        provider.ask("s", "p", picture, None, 8)

    # In a conversion a region whose every call fails is left alone and the run finishes.
    real = enrich.make_provider

    def make(name, **kw):
        p = real(name, **kw)
        if name == "ollama":
            def boom(*a, **k):
                raise ProviderError("out of memory on cpu for this image")
            p.client.ask = boom
        return p

    monkeypatch.setattr("reflow.pipeline.make_provider", make)
    monkeypatch.setattr("reflow.prepare.make_runner", lambda _: FakeRunner("1234 #%&"))
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan"])
    summary = convert(pdf, tmp_path / "out", assist="local", page_images=False)
    assert summary.assist["failed"] == 1 and summary.assist["drafted"] == 0


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    return buf.getvalue()


class _Encoded(dict):
    def to(self, device):
        return self


class _Processor:
    def apply_chat_template(self, *a, **kw):
        return _Encoded(input_ids=[[1]])


class _Model:
    def __init__(self, error):
        self.error = error

    def generate(self, **kw):
        raise self.error


class FakeOllama:
    """vision.send for one Ollama /api/chat reply; keeps what was sent, and whether it could
    connect only to this machine."""

    def __init__(self, reply):
        self.reply, self.sent, self.loopback = reply, [], []

    def __call__(self, request, timeout=None, loopback=False):
        self.sent.append(json.loads(request.data))
        self.loopback.append(loopback)
        return io.BytesIO(json.dumps(self.reply).encode())


def test_ollama_transcriber_asks_the_assist_prompt_for_the_same_json(monkeypatch):
    fake = FakeOllama({"message": {"content": '{"text": "Tf = -1.39 °C", "handwritten": true}'},
                       "done_reason": "stop"})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    model = assist.OllamaTranscriber("openbmb/minicpm-o4.5", host="h:1")
    assert model.transcribe(b"png", "image/png") == Transcription("Tf = -1.39 °C", True)
    sent = fake.sent[0]
    assert sent["messages"][0] == {"role": "system", "content": assist.TRANSCRIPTION_PROMPT}
    assert sent["format"] == assist.TRANSCRIPTION_SCHEMA and sent["think"] is False
    assert model.cache_id == "ollama:openbmb/minicpm-o4.5"


def test_ollama_ocr_prompt_sends_the_bare_prompt_and_leaves_handwriting_to_the_region(monkeypatch):
    fake = FakeOllama({"message": {"content": "  ADOPTED this 4th day\n"}, "done_reason": "stop"})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    model = assist.OllamaTranscriber("minicpm-ocr", ocr_prompt=True)
    assert model.transcribe(b"png", "image/png") == Transcription("ADOPTED this 4th day", False)
    sent = fake.sent[0]
    assert [m["role"] for m in sent["messages"]] == ["user"]
    assert sent["messages"][0]["content"] == assist.OCR_PROMPT and "format" not in sent
    assert model.cache_id == "ollama:minicpm-ocr:ocr"   # never shares a cache entry with JSON


@pytest.mark.parametrize("ocr_prompt", [False, True])
def test_ollama_transcription_cut_at_the_token_limit_is_discarded(monkeypatch, ocr_prompt):
    fake = FakeOllama({"message": {"content": '{"text": "87/88-1 87/88-1 87/88-1'},
                       "done_reason": "length"})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    with pytest.raises(ProviderError, match="token limit"):
        assist.OllamaTranscriber("qwen3-vl:8b", ocr_prompt=ocr_prompt).transcribe(b"png", "image/png")


# -- the checks on a reading of an unread crop (#343) ------------------------------------------

def test_a_reading_is_checked_against_its_crop():
    ok = "5/30/25 Austin, Texas"
    assert assist.check(ok, 2000, writing=True) == []
    # Nothing but the prompt's marker, or nothing at all, reads no writing.
    assert assist.check("[illegible]\n[illegible]", 2000, writing=True) == ["nothing"]
    assert assist.check("", 2000, writing=True) == ["nothing"]
    # ...which is a valid reading of a whole page with no text.
    assert assist.check("", 2000, writing=False) == []
    # A repetition loop: a run of words back to back, more than three times.
    assert assist.check("87/88-1 87/88-1 87/88-1 87/88-1", 2000, writing=True) == ["repeated"]
    loop = "| Allstate | 5127 Orange Ave. |\n" + "| Apple Press | Advance Design |\n" * 4
    assert assist.check(loop, 20_000, writing=True) == ["repeated"]
    assert assist.check("| 0 | 0 | 0 | 0 | 0 |", 2000, writing=True) == []   # too short to loop
    # More letters than the crop's ink can hold: 80 pixels hold at most 20.
    assert assist.check("x" * 21, 80, writing=True) == ["invented"]
    assert assist.check("x" * 20, 80, writing=True) == []


def test_ink_counts_the_crops_dark_pixels():
    assert assist.ink(png(dark=0)) == 0 and assist.ink(png(dark=345)) == 345


def unread_page(*kinds):
    crops = [Node(Role.FIGURE, page=1, bbox=(10.0, 20.0 + 100 * i, 200.0, 60.0 + 100 * i),
                  attrs={"unread": True, "kind": "handwriting", "handwritten": True,
                         "alt": "Handwriting, not transcribed."}) for i, _ in enumerate(kinds)]
    doc = document()
    doc.root.children[1:1] = crops
    doc.ensure_unique_ids()
    return doc, [(Region(1, c.bbox, "unread-figure", True, c.id), png(c.id)) for c in crops]


class Answers(FakeModel):
    """Answers in turn, the last one again once they run out."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def transcribe(self, image, media_type, told=""):
        self.result = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return super().transcribe(image, media_type, told)


def test_a_rejected_crop_is_read_once_more_told_why_and_a_passing_reading_goes_under_it(tmp_path):
    doc, candidates = unread_page("handwriting")
    model = Answers(Transcription("[illegible]", True), Transcription("Sara Smith", True))
    stats = draft(doc, candidates, model, tmp_path / "cache.json")
    crop, below = doc.root.children[1], doc.root.children[2]
    assert model.told == ["", "Read it again: the last answer read no text."]
    assert stats == {"drafted": 1, "cached": 0, "rejected": 0, "failed": 0}
    assert below.text == "Sara Smith" and below.attrs["assist_for"] == crop.id
    assert crop.attrs["reading"] == "drafted" and crop.attrs["alt"] == "Handwriting."
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text()
    assert "Not transcribed" not in html and "Handwritten · AI-drafted, not reviewed" in html
    # A replay reaches the same verdict from the cache, with no call.
    again, candidates = unread_page("handwriting")
    replay = Answers(Transcription("never asked", True))
    assert draft(again, candidates, replay, tmp_path / "cache.json")["cached"] == 1
    assert replay.calls == [] and again.root.children[2].text == "Sara Smith"


def test_a_crop_no_reading_passes_stays_and_says_why(tmp_path):
    doc, candidates = unread_page("handwriting")
    model = Answers(Transcription("Sara Sara Sara Sara Sara", True))
    stats = draft(doc, candidates, model, tmp_path / "cache.json")
    crop = doc.root.children[1]
    assert len(model.calls) == 2 and stats["rejected"] == 1
    assert [n.text for n in doc.root.children] == [
        "OCR baseline", None, "Next page"]     # OCR's baseline, and the crop, as they were
    assert crop.attrs["reading"] == "rejected" and crop.attrs["alt"] == "Handwriting, not transcribed."
    assert crop.attrs["why"][-1] == "reading: rejected (repeated)"
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    assert ("Not transcribed: no model reading passed the checks. Check the original page."
            in (tmp_path / "index.html").read_text())


def test_an_unavailable_model_is_asked_once_and_every_crop_says_so(tmp_path):
    from reflow.analyze.vision import ProviderUnavailable
    doc, candidates = unread_page("handwriting", "handwriting")
    model = FakeModel(ProviderUnavailable("Ollama is not reachable"))
    stats = draft(doc, candidates, model, tmp_path / "cache.json")
    assert len(model.calls) == 1 and stats["failed"] == 2
    crops = [n for n in doc.root.children if n.role == Role.FIGURE]
    assert [c.attrs["reading"] for c in crops] == ["unavailable"] * 2
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    assert (tmp_path / "index.html").read_text().count(
        "Not transcribed: the model was unavailable. Check the original page.") == 2


def test_with_no_model_asked_the_crop_says_so(tmp_path):
    doc, _ = unread_page("handwriting")
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    assert ("Not transcribed: no model was asked to read it. Check the original page."
            in (tmp_path / "index.html").read_text())


def test_a_page_with_unread_crops_sends_them_not_the_page_and_a_signature_only_when_asked(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["scan+text"])
    doc = ingest(pdf)
    doc.pages[0].facts.append(Fact("scan-noisy", Source.HEURISTIC, ""))
    pen = _unread_figure(1, (100.0, 400.0, 300.0, 460.0), True)
    signature = Node(Role.FIGURE, page=1, bbox=(100.0, 500.0, 300.0, 560.0),
                     attrs={"unread": True, "kind": "signature", "handwritten": True,
                            "signs": "Jane Doe"})
    doc.root.children += [pen, signature]
    assert [r for r, _ in regions(doc, pdf)] == [Region(1, pen.bbox, "unread-figure", True, pen.id)]
    assert [r for r, _ in regions(doc, pdf, signatures=True)] == [
        Region(1, pen.bbox, "unread-figure", True, pen.id),
        Region(1, signature.bbox, "signature", True, signature.id, "Jane Doe")]
    # A page whose only crop is a signature is not sent whole either.
    doc.root.children.remove(pen)
    assert regions(doc, pdf) == []
    assert [r.reason for r, _ in regions(doc, pdf, signatures=True)] == ["signature"]


# -- a signature's name (#345) -------------------------------------------------------------------

# Words a form prints on or under a signature line that name no one (the verifier's list).
ROLES = ["President", "Secretary", "Date", "Signature", "Print Name", "Applicant", "Witness",
         "Title", "Owner", "Name", "Applicant Signature", "Signature Date", "Owner Title",
         "Witness Signature", "Speaker of the House", "President of the Senate", "Chief Executive",
    "Chief Executive Officer", "Chief Financial Officer", "Please sign below", "Please Below",
    "Authorized Representative", "Full Legal Name", "Sign Here", "Board Member"]
# A model's way of saying it read no name.
NON_ANSWERS = ["Unknown", "No signature", "Not legible", "Cannot read", "Can't read", "Unreadable",
               "[illegible]", "illegible", "Sara [illegible]", "None", "N/A", "No name", "Unclear"]


def test_a_signature_is_named_only_when_its_printed_line_has_the_name_in_a_name_field():
    assert assist.signer("Sara Dieinger", "Name: Sara Dieringer") == "Sara Dieringer"  # as printed
    assert assist.signer("Angela  Elliott\n", "Print Name: Angela Elliott") == "Angela Elliott"
    assert assist.signer("angela elliott", "Printed Name Angela Elliott") == "Angela Elliott"
    assert assist.signer("J. Smith", "By: John Smith, Treasurer") == "J. Smith"
    assert assist.signer("Jane Doe", "Name of Applicant: Jane Doe") == "Jane Doe"
    # Printed words with no name field could be a role as well as a name: no name.
    for printed in ("Angela Elliott", "Angela Elliott, Secretary", "Managing Attorney",
                    "Human Resources"):
        assert assist.signer(printed, printed) is None, printed
    # The name is the field's own value: a blank field names no one, whatever follows it.
    for printed in ("Name: ______ Title: Managing Attorney", "Name: Title: Managing Attorney",
                    "Name ______ Managing Attorney", "Print Name: (please print) Managing Attorney"):
        assert assist.signer("Managing Attorney", printed) is None, printed
    # No printed name: "Signature" alone, however the model reads it. These are the verifier's
    # confident readings of real crops: another signer's label (0.907) and a misreading (0.82).
    for reading in ("Angela Elliott", "King", "Sara Dienger", "Phil King"):
        assert assist.signer(reading) is None and assist.signer(reading, "President") is None
    assert assist.signer("Jane Roe", "Jane Doe") is None
    assert assist.signer("Tan Parker", "Phil King") is None


def test_a_name_is_one_printed_name_whole_never_pieced_from_two():
    assert assist.signer("John Doe", "John Smith/Jane Doe") is None
    assert assist.signer("John Doe", "John Smith, Jane Doe") is None
    assert assist.signer("John Doe", "John Smith Jane Doe") is None
    assert assist.signer("Smith John", "John Smith") is None          # in the printed order
    assert assist.signer("John Doe", "Name: John Smith/Jane Doe") is None
    assert assist.signer("Jane Doe", "Name: John Smith/Jane Doe") is None   # the field's name only
    assert assist.signer("John Smith", "Name: John Smith/Jane Doe") == "John Smith"


def test_a_role_or_form_word_is_never_a_name():
    for role in ROLES:
        assert assist.signer(role, role) is None, role
        assert assist.signer(role, f"{role} Jane Doe") is None, role
        assert assist.signer(role, f"Name: {role}") is None, role
    assert assist.signer("Chief Executive", "Chief Executive Officer") is None
    assert assist.signer("Please Below", "Please sign below") is None
    # A name with a role in it is not a name either.
    assert assist.signer("Jane President", "Name: Jane Doe, President") is None
    assert assist.signer("Mr. Doe", "Name: Mr. John Doe") is None


def test_a_first_name_alone_is_never_a_name():
    assert assist.signer("John", "Name: John Smith") is None
    assert assist.signer("Smith", "Name: John Smith") is None
    assert assist.signer("John John", "Name: John Smith") is None   # one printed word, one word
    assert assist.signer("John", "Name: John") is None
    assert assist.signer("J. S.", "Name: John Smith") is None


def test_a_non_answer_is_never_a_name():
    for answer in NON_ANSWERS:
        assert assist.signer(answer, answer) is None, answer
        assert assist.signer(answer, f"{answer} Jane Doe") is None, answer
    for answer in ["", "UtaScore 40", "B o frrr r", "Signed by: Sara", "A B C D E",
                   "Phil\nKing\nKing\nKing\nKing"]:
        assert assist.signer(answer, answer) is None, answer


class Namer(FakeModel):
    """A local model that reads signatures: each answer in turn."""

    def __init__(self, *answers):
        super().__init__()
        self.answers, self.named = list(answers), []

    def read_signature(self, image, media_type):
        self.named.append(image)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def signature_page(*signs):
    crops = [Node(Role.FIGURE, page=1, bbox=(10.0, 20.0 + 100 * i, 200.0, 60.0 + 100 * i),
                  attrs={"unread": True, "kind": "signature", "handwritten": True,
                         "alt": "Signature."}) for i, _ in enumerate(signs)]
    doc = document()
    doc.root.children[1:1] = crops
    doc.ensure_unique_ids()
    return doc, [(Region(1, c.bbox, "signature", True, c.id, s), png(c.id))
                 for c, s in zip(crops, signs, strict=True)]


def test_a_signature_named_by_the_model_has_its_name_drafted_under_it(tmp_path):
    doc, candidates = signature_page("Print Name: Angela Elliott", "Secretary")
    model = Namer("Angela Elliott", "Phil Rina")
    stats = draft(doc, candidates, model, tmp_path / "cache.json")
    assert stats == {"drafted": 1, "cached": 0, "rejected": 1, "failed": 0}
    assert model.calls == []           # a signature is asked for its name, never transcribed
    named, below, unnamed = doc.root.children[1:4]
    assert below.text == "Angela Elliott" and below.attrs["assist_for"] == named.id
    assert below.attrs["provenance"] == "ai-drafted" and below.attrs["handwritten"] is True
    assert [named.attrs["reading"], unnamed.attrs["reading"]] == ["drafted", "rejected"]
    assert unnamed.attrs["why"][-1] == "reading: rejected ('Phil Rina', no printed name like it)"
    assert [n.attrs["alt"] for n in (named, unnamed)] == ["Signature.", "Signature."]
    assert any(i["id"] == below.id and i["label"] == "AI-drafted, not reviewed" for i in queue(doc))
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text()
    assert "Not transcribed" not in html and html.count('aria-label="Signature."') == 2
    assert "Handwritten · AI-drafted, not reviewed" in html and "Phil Rina" not in html
    # A replay names it from the cache, with no call.
    again, candidates = signature_page("Print Name: Angela Elliott", "Secretary")
    replay = Namer()
    assert draft(again, candidates, replay, tmp_path / "cache.json")["cached"] == 1
    assert replay.named == [] and again.root.children[2].text == "Angela Elliott"


def test_an_unavailable_model_leaves_the_signature_alone(tmp_path):
    from reflow.analyze.vision import ProviderUnavailable
    doc, candidates = signature_page(None, None)
    model = Namer(ProviderUnavailable("Ollama is not reachable"))
    stats = draft(doc, candidates, model, tmp_path / "cache.json")
    assert len(model.named) == 1 and stats["failed"] == 2
    crops = [n for n in doc.root.children if n.role == Role.FIGURE]
    assert [c.attrs["reading"] for c in crops] == ["unavailable"] * 2
    assert len(doc.root.children) == 4
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    assert "Not transcribed" not in (tmp_path / "index.html").read_text()


def test_a_signature_reads_as_signature_with_no_model(tmp_path):
    doc, _ = signature_page(None)
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text()
    assert 'aria-label="Signature."' in html and "Not transcribed" not in html


def test_ollama_reads_a_signature_only_on_this_machine(monkeypatch):
    fake = FakeOllama({"message": {"content": "Angela Elliott\n"}, "done_reason": "stop"})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    resolving(monkeypatch, {"localhost": ["127.0.0.1", "::1"], "10.0.0.5": ["10.0.0.5"],
                            "127.a.8.8.8.8.nip.io": ["8.8.8.8"]})
    assert assist.OllamaTranscriber(TRANSCRIPTION_MODEL).read_signature(b"png", "image/png") == (
        "Angela Elliott")
    sent = fake.sent[0]
    assert sent["messages"][0]["content"] == assist.SIGNATURE_PROMPT and "format" not in sent
    assert "logprobs" not in sent and sent["options"]["temperature"] == 0
    assert fake.loopback == [True]
    # Asked directly, past assist.names_signatures, a server elsewhere still gets no signature.
    for host in ("http://10.0.0.5:11434", "127.a.8.8.8.8.nip.io:11434"):
        for transcriber in (assist.OllamaTranscriber(TRANSCRIPTION_MODEL, host=host),
                            enrich.ModelProvider("ollama", OllamaVision(TRANSCRIPTION_MODEL, host=host))):
            with pytest.raises(ProviderUnavailable, match="this machine"):
                transcriber.read_signature(b"png", "image/png")
    assert len(fake.sent) == 1


def test_a_signature_goes_only_to_a_model_on_this_machine(tmp_path, monkeypatch):
    import contextlib
    from types import SimpleNamespace
    monkeypatch.delenv("OLLAMA_HOST", raising=False)   # the default host is this machine
    resolving(monkeypatch, {"localhost": ["127.0.0.1", "::1"], "10.0.0.5": ["10.0.0.5"],
                            "gpu.example": ["10.0.0.6"], "127.a.8.8.8.8.nip.io": ["8.8.8.8"]})
    asked = []
    monkeypatch.setattr(assist, "regions", lambda doc, working, signatures=False:
                        asked.append(signatures) or [])
    run = SimpleNamespace(doc=document(), working=tmp_path / "x.pdf", stage=tmp_path, meta={},
                          step=lambda name: contextlib.nullcontext(),
                          workers=SimpleNamespace(pdfium=contextlib.nullcontext()),
                          progress=lambda *a: None)
    pytest.importorskip("torch")
    from reflow.analyze.local_model import LocalModel
    from reflow.analyze.vision import AnthropicVision, OllamaVision
    for transcriber in (enrich.make_provider("ollama", model=TRANSCRIPTION_MODEL),  # --assist local
                        assist.OllamaTranscriber(TRANSCRIPTION_MODEL),
                        assist.OllamaTranscriber(TRANSCRIPTION_MODEL, host="http://10.0.0.5:11434"),
                        enrich.ModelProvider("ollama", OllamaVision("m", host="gpu.example:11434")),
                        assist.OllamaTranscriber(TRANSCRIPTION_MODEL,
                                                 host="127.a.8.8.8.8.nip.io:11434"),
                        enrich.ModelProvider("anthropic", AnthropicVision("claude", object())),
                        enrich.ModelProvider("local", LocalModel()),
                        FakeModel()):
        assist.transcribe(run, transcriber)
    assert asked == [True, True, False, False, False, False, False, False]


# -- a reading that holds math (#346) ---------------------------------------------------------

def test_the_prompt_asks_for_math_as_latex_between_dollar_signs_and_gives_no_formula_to_copy():
    prompt = assist.TRANSCRIPTION_PROMPT
    assert "LaTeX between dollar signs" in prompt
    # A prompt with a whole formula in it was copied into the reading of a crop that held none.
    assert "$" not in prompt and "=" not in prompt


def test_a_latex_commands_letters_are_not_counted_against_the_crops_ink():
    reading = r"$\mathrm{Na}_2 \times \frac{a}{b}$"
    assert assist._alnum(reading) > 40 / assist.INK_PER_LETTER + assist.INK_SLACK
    assert assist.check(reading, 40, writing=True) == []
    assert assist.check("x" * 21, 80, writing=True) == ["invented"]
    # A formula of Greek letters alone is writing: it has no letter but its commands.
    assert assist.check(r"$\alpha = \beta$", 2000, writing=True) == []


def test_a_drafted_formula_is_math_under_its_crop_with_the_reading_still_there_to_check(tmp_path):
    doc, candidates = unread_page("handwriting", "handwriting")
    model = Answers(Transcription(r"$T_f = -7.48^\circ\mathrm{C}$", True),
                    Transcription(r"$\begin{x}$ 1.39 and $\frac{1}{2}$", True))
    draft(doc, candidates, model, tmp_path / "cache.json")
    drafts = [n for n in doc.root.walk() if n.source == Source.LLM]
    assert [n.text for n in drafts] == [r"$T_f = -7.48^\circ\mathrm{C}$",
                                        r"$\begin{x}$ 1.39 and $\frac{1}{2}$"]
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text()
    assert ("<math><msub><mi>T</mi><mi>f</mi></msub><mo>=</mo><mo>-</mo>"
            "<msup><mn>7.48</mn><mo>°</mo></msup><mi mathvariant=\"normal\">C</mi></math>") in html
    # Still a draft: its badge, its crop and its provenance stay. A formula that does not read
    # keeps the text the model wrote, escaped.
    assert html.count('data-provenance="ai-drafted"') == 2
    assert html.count("Handwritten · AI-drafted, not reviewed") == 2
    assert r"$\begin{x}$ 1.39 and <math><mfrac>" in html
    assert "Not transcribed" not in html
    assert Document.from_json(doc.to_json()).to_dict() == doc.to_dict()


def test_a_readings_math_is_not_markup_whatever_the_model_wrote(tmp_path):
    doc, candidates = unread_page("handwriting")
    model = Answers(Transcription(r"$\text{<img src=x onerror=alert(1)>}$ <script>x</script> $y$", True))
    draft(doc, candidates, model, tmp_path / "cache.json")
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text()
    assert "<img src=x" not in html and "<script>x" not in html
    assert "<mtext>&lt;img src=x onerror=alert(1)&gt;</mtext>" in html


def test_a_default_conversion_has_no_math_and_a_plain_reading_is_set_as_before(tmp_path):
    doc, candidates = unread_page("handwriting")
    draft(doc, candidates, Answers(Transcription("5/30/25 costs $5 to $10", True)), tmp_path / "cache.json")
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text()
    assert "<math" not in html and "5/30/25 costs $5 to $10" in html
