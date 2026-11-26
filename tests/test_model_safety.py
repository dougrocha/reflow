import ast
import inspect
import re
from html.parser import HTMLParser

import pytest

from reflow.analyze.sections import heading_of
from reflow.model import Node, NodeAttrs, Role
from reflow.model import Document
from reflow.render import chunks, html, links
from reflow.render.links import attribute
from reflow.render.passages import passages


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_attrs_round_trip():
    attrs: NodeAttrs = {"fid": "f1-2", "kind": "text", "width": 30.5,
                        "lines": ["Exact Case"], "required": True}
    node = Node(Role.FIELD, attrs=attrs)
    assert Node.from_dict(node.to_dict()).to_dict() == node.to_dict()
    assert node.to_dict()["attrs"] == attrs


@pytest.mark.parametrize("name", ["id", "scope", "lang", "data-from", "href"])
def test_none_attributes_raise_in_tests(name):
    with pytest.raises(ValueError, match="cannot be None"):
        attribute(name, None)
    with pytest.raises(ValueError, match="cannot be None"):
        attribute(name, "prefix", None, "suffix")


def test_missing_fid_is_rejected():
    with pytest.raises(ValueError, match="attribute 'id' cannot be None"):
        html.NodeRenderer({}, {}).render(Node(Role.FIELD, attrs={"kind": "text"}))


def test_production_omits_none_with_warning(monkeypatch):
    monkeypatch.delenv("REFLOW_STRICT_HTML_ATTRIBUTES")
    with pytest.warns(RuntimeWarning, match="cannot be None"):
        rendered = html.NodeRenderer({}, {}).render(
            Node(Role.FIELD, text="Printed label", attrs={"kind": "text"}))
    assert "None" not in rendered
    assert "Printed label" in rendered
    assert all("id" not in attrs and "for" not in attrs for _, attrs in Tags(rendered).tags)


@pytest.mark.parametrize("children", [[], [Node(Role.P, text="Body")]])
def test_sections_require_heading(children):
    section = Node(Role.SECTION, children=children)
    for action in (lambda: heading_of(section),
                   lambda: html.NodeRenderer({}, {}).render(section),
                   lambda: chunks.split_by_page([section], {1: []}),
                   lambda: list(passages(Document(root=Node(Role.DOC, children=[section]))))):
        with pytest.raises(ValueError, match="must have a heading as its first child"):
            action()


@pytest.mark.parametrize("name", ["data-from", "scope", "lang", "id", "href", "title"])
def test_attributes_round_trip_special_characters(name):
    value = "\"<>&'"
    parsed = Tags(f"<span {attribute(name, value)}></span>")
    assert parsed.tags == [("span", {name: value})]


def test_dynamic_quoted_attributes_use_writer():
    # Check the actual f-string AST, including multiline and nested expressions.
    # Constants may contain literal attributes; interpolated values use attribute().
    for module in (html, chunks, links):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for before, value in zip(node.values, node.values[1:]):
                if isinstance(before, ast.Constant) and isinstance(value, ast.FormattedValue):
                    assert not re.search(r"""[\w-]+=(?:"[^"]*|'[^']*)$""", before.value), ast.unparse(node)


def test_validation_does_not_depend_on_assert():
    from pathlib import Path
    import reflow
    root = Path(reflow.__file__).parent
    assert not [(str(p), n.lineno) for p in root.rglob("*.py")
                for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, ast.Assert)]
