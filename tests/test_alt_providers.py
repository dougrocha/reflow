import io
import json
import zlib
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name
from typer.testing import CliRunner

from reflow.providers import ALT_TEXT_MODELS, TRANSCRIPTION_MODEL
from reflow import convert as convert_module
from reflow import pipeline
from reflow.analyze import enrich
from reflow.analyze.vision import AnthropicVision
from reflow.cli import app
from reflow.model import Document, Role
from reflow.run import Options

ANSWER = {"kind": "photo", "decorative": False, "alt": "A pump beside a tank.",
          "description": "", "uncertain": False}


def photo_pdf(path: Path) -> Path:
    """One text page with a 200 x 150 pt photo-like picture (a gradient, so its crop is not blank)."""
    w, h = 200, 150
    pixels = bytes((x + y) % 256 for y in range(h) for x in range(w))
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    image = pikepdf.Stream(pdf, zlib.compress(pixels), Type=Name.XObject, Subtype=Name.Image,
                           Width=w, Height=h, ColorSpace=Name.DeviceGray, BitsPerComponent=8,
                           Filter=Name.FlateDecode)
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im0=image))
    page.Contents = pdf.make_stream(
        b"BT /F1 12 Tf 72 720 Td (The treatment plant in 2024) Tj ET\n"
        b"q 200 0 0 150 100 400 cm /Im0 Do Q\n")
    pdf.save(path)
    return path


class FakeHttp:
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def __call__(self, request, timeout=None, loopback=False):
        self.requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps(self.reply).encode())


def test_the_alt_text_and_transcription_providers_are_built_independently(monkeypatch, tmp_path):
    built = []

    class Transcriber:
        name, cache_id, confidence, workers = "local", "fake", 0.5, 1

        def close(self):
            pass

    def make(name, **kw):
        built.append((name, kw.get("model")))
        return Transcriber() if kw.get("model") == TRANSCRIPTION_MODEL else enrich.make_provider(name, **kw)

    fake = FakeHttp({"message": {"content": json.dumps(ANSWER)}})
    monkeypatch.setattr(pipeline, "make_provider", make)
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    monkeypatch.setattr(Options, "uses_model", property(lambda self: False))   # no torch here
    out = tmp_path / "out"
    convert_module.convert(photo_pdf(tmp_path / "p.pdf"), out, assist="local",
                           alt_text="ollama", page_images=False)
    assert built == [("ollama", TRANSCRIPTION_MODEL), ("ollama", None)]
    assert {r["model"] for r in fake.requests} == {ALT_TEXT_MODELS["ollama"]}
    doc = Document.from_json((out / "document.json").read_text())
    [figure] = [n for n in doc.root.walk() if n.role == Role.FIGURE]
    assert figure.attrs["alt"] == ANSWER["alt"]


@pytest.mark.parametrize("command", ["convert", "convert-many"])
def test_alt_text_model_reaches_the_ollama_request(monkeypatch, tmp_path, command):
    fake = FakeHttp({"message": {"content": json.dumps(ANSWER)}})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    pdf = photo_pdf(tmp_path / "p.pdf")
    out = tmp_path / "out"
    args = [command, str(pdf), *(["-o", str(out)] if command == "convert" else [str(out)])]
    result = CliRunner().invoke(app, [*args, "--alt-text", "ollama", "--alt-text-model", "m:1",
                                     "--jobs", "1", "--no-page-images"])
    assert result.exit_code == 0, result.output
    assert fake.requests and all(r["model"] == "m:1" for r in fake.requests)
    doc = Document.from_json((out / "document.json").read_text())
    figure = next(n for n in doc.root.walk() if n.role == Role.FIGURE)
    assert figure.attrs["provenance"] == "ai-drafted" and figure.attrs["alt"] == ANSWER["alt"]
    assert figure.attrs["figure_role"] == "content"
    assert figure.attrs["warn"] == "suggested-alt"
    review = json.loads((out / "review.json").read_text())
    drafted = [i for i in review if i.get("alt") == ANSWER["alt"]]
    assert [(i["warn"], i["needs"]) for i in drafted] == [("ai-drafted", None)]
    assert not any(i.get("needs") == "alt" for i in review)


def test_a_figure_records_the_hash_of_its_crop_when_the_crop_is_made(monkeypatch, tmp_path):
    from reflow.analyze.anchors import crop_sha
    from reflow.names import FIGURES_DIR
    monkeypatch.setattr("reflow.analyze.vision.send",
                        FakeHttp({"message": {"content": json.dumps(ANSWER)}}))
    out = tmp_path / "out"
    convert_module.convert(photo_pdf(tmp_path / "p.pdf"), out, alt_text="ollama",
                           page_images=False)
    doc = Document.from_json((out / "document.json").read_text())
    [figure] = [n for n in doc.root.walk() if n.role == Role.FIGURE]
    crops = sorted((out / FIGURES_DIR).iterdir())
    assert [crop_sha(c.read_bytes()) for c in crops] == [figure.attrs["crop_sha"]]
    review = json.loads((out / "review.json").read_text())
    anchors = [i["anchor"] for i in review if i["id"] == figure.id]
    assert anchors and anchors[0]["crop_sha"] == figure.attrs["crop_sha"]


def test_the_providers_and_their_options():
    assert isinstance(enrich.make_provider("anthropic").client, AnthropicVision)
    assert enrich.make_provider("ollama", model="x:2").cache_id == "ollama:x:2"
    with pytest.raises(ValueError, match="unknown alt-text provider"):
        Options(alt_text="claude-cli")
    with pytest.raises(ValueError, match="needs --alt-text"):
        Options(alt_text_model="m")
    with pytest.raises(ValueError, match="blank"):
        Options(alt_text="ollama", alt_text_model=" ")
    assert not Options(alt_text="ollama").uses_model and not Options(alt_text="anthropic").uses_model
