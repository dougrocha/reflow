import json
import re

import pytest

from reflow.model import Document, Node, PageInfo, Role
from reflow.analyze.review import queue as review_items
from reflow.render.site import render_html
from reflow.analyze.language import (
    STOPWORDS,
    block_language,
    detect_latin_language,
    document_language,
    is_rtl_text,
    normalize_tag,
    page_languages,
    resolve_language,
    second_block_language,
)

SPANISH = ("Las vacunas son seguras y eficaces para los niños. Si usted tiene preguntas, "
           "hable con su médico. Es importante que usted y sus hijos se vacunen a tiempo.")
ENGLISH = ("The vaccines are safe and effective for the children. If you have questions, "
           "talk to your doctor or nurse. It is important that you and your children get "
           "them on time.")


@pytest.mark.parametrize("value, tag", [
    ("en-US", "en-US"),       # well-formed tags are kept as written
    ("es", "es"),
    ("ko", "ko"),
    ("ar", "ar"),
    ("zh-Hant-TW", "zh-Hant-TW"),
    ("EN", "EN"),             # case-insensitive in BCP 47, so kept
    ("en_US", "en-US"),       # underscore is not a tag separator
    ("en US", "en-US"),
    ("  fr-CA  ", "fr-CA"),
    ("Korean", "ko"),         # English language names map to their tags
    ("Spanish", "es"),
    ("Arabic", "ar"),
    ("English", "en"),
    ("Hebrew", "he"),
    ("Korean (South Korea)", "ko"),
    ("Lao", "lo"),            # a name wins over the 3-letter code it resembles
    ("Klingon", None),        # neither a tag nor a known name
    ("x-private", None),      # a private-use tag names no language
    ("", None),
    (None, None),
])
def test_normalize_tag(value, tag):
    assert normalize_tag(value) == tag


def doc_with(nodes, lang="en-US"):
    return Document(root=Node(Role.DOC, children=nodes),
                    pages=[PageInfo(1, 612, 792)], meta={"lang": lang})


def test_language_from_metadata_or_script_when_metadata_is_unusable():
    assert document_language(doc_with([], lang="Korean")) == ("ko", False)
    assert document_language(doc_with([], lang=None)) == ("en", False)
    hangul = doc_with([Node(Role.P, page=1, text="안녕하세요 여러분")], lang=None)
    assert document_language(hangul) == ("ko", False)
    arabic = doc_with([Node(Role.P, page=1, text="مرحبا بكم في العيادة")], lang="Klingon")
    assert document_language(arabic) == ("ar", True)
    hebrew = doc_with([Node(Role.P, page=1, text="שלום לכולם")], lang=None)
    assert document_language(hebrew) == ("he", True)


def test_metadata_language_wins_over_script():
    arabic_text = doc_with([Node(Role.P, page=1, text="مرحبا بكم")], lang="en-GB")
    assert document_language(arabic_text) == ("en-GB", True)  # RTL script still counts


def test_rtl_text_is_decided_by_its_letters():
    assert is_rtl_text("مرحبا بكم في العيادة")
    assert is_rtl_text("Fig. 2 (مرحبا بكم)")          # mostly Arabic letters
    assert not is_rtl_text("Visit the clinic at 5 pm")
    assert not is_rtl_text("2026 - 1234 !!")           # no letters at all
    assert not is_rtl_text("Dr Smith: مرحبا")           # more Latin letters than Arabic


def write(tmp_path, doc):
    render_html(doc, "Clinic", tmp_path, page_widths=(800,))
    return (tmp_path / "index.html").read_text()


def test_korean_metadata_renders_as_ko(tmp_path):
    index = write(tmp_path, doc_with([Node(Role.P, page=1, text="안녕하세요")], lang="Korean"))
    assert '<html lang="ko">' in index
    assert 'dir="rtl"' not in index


def test_arabic_document_is_rtl_on_html_only(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text="مرحبا بكم في العيادة الصحية")], lang="ar")
    index = write(tmp_path, doc)
    assert '<html lang="ar" dir="rtl">' in index
    # The whole document is right-to-left, so its blocks do not repeat the direction.
    assert index.count('dir="rtl"') == 1


def test_manifest_records_language_and_direction_of_the_document(tmp_path):
    arabic = doc_with([Node(Role.P, page=1, text="مرحبا بكم في العيادة الصحية")], lang="ar")
    korean = doc_with([Node(Role.P, page=1, text="안녕하세요")], lang="Korean")
    for name, doc in [("arabic", arabic), ("korean", korean)]:
        render_html(doc, "Clinic", tmp_path / name, page_widths=(800,))
    ar = json.loads((tmp_path / "arabic" / "manifest.json").read_text())
    ko = json.loads((tmp_path / "korean" / "manifest.json").read_text())
    assert (ar["lang"], ar["dir"]) == ("ar", "rtl")
    assert (ko["lang"], ko["dir"]) == ("ko", "ltr")



def test_arabic_paragraph_in_english_document_is_rtl_on_the_block_only(tmp_path):
    doc = doc_with([
        Node(Role.H2, page=1, text="Getting tested"),
        Node(Role.P, page=1, text="Ask your doctor about a hepatitis B test."),
        Node(Role.P, page=1, text="مرحبا بكم في العيادة الصحية"),
        Node(Role.LIST, page=1, children=[
            Node(Role.LI, page=1, text="أشخاص من مواليد أفريقيا"),
            Node(Role.LI, page=1, text="Anyone with a family history"),
        ]),
    ])
    index = write(tmp_path, doc)
    head = index.split("<body", 1)[0]
    assert '<html lang="en-US">' in head and "dir=" not in head
    assert re.search(r'<h2 id="[^"]*" data-node="[^"]*">Getting tested', index)
    assert len(re.findall(r'<p dir="rtl" data-node="[^"]*">مرحبا', index)) == 1
    assert re.search(r'<li dir="rtl" data-node="[^"]*">أشخاص', index)
    assert len(re.findall(r'dir="rtl"', index)) == 2  # the paragraph and one list item


def test_spanish_text_without_lang_is_es():
    doc = doc_with([Node(Role.P, page=1, text=SPANISH)], lang=None)
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue) == ("es", "language-missing")
    assert document_language(doc) == ("es", False)


def test_english_text_with_en_us_is_unchanged():
    doc = doc_with([Node(Role.P, page=1, text=ENGLISH)], lang="en-US")
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue) == ("en-US", None)
    assert choice.detected == "en"


def test_english_text_with_contradicting_lang_is_en_and_reported():
    doc = doc_with([Node(Role.P, page=1, text=ENGLISH)], lang="es")
    choice = resolve_language(doc)
    assert (choice.tag, choice.declared, choice.issue) == ("en", "es", "language-mismatch")
    assert document_language(doc) == ("en", False)


def test_the_last_answer_is_reused_only_for_the_same_text_and_lang():
    doc = doc_with([Node(Role.P, page=1, text=SPANISH)], lang=None)
    first = resolve_language(doc)
    assert resolve_language(doc_with([Node(Role.P, page=1, text=SPANISH)], lang=None)) is first
    other = resolve_language(doc_with([Node(Role.P, page=1, text=ENGLISH)], lang=None))
    assert (first.tag, other.tag) == ("es", "en")
    assert resolve_language(doc_with([Node(Role.P, page=1, text=ENGLISH)], lang="es")).tag == "en"
    assert resolve_language(doc_with([Node(Role.P, page=1, text=ENGLISH)], lang="en-US")).tag == "en-US"
    doc.root.children[0].set_text(ENGLISH)   # the same document, edited between two asks
    assert resolve_language(doc).tag == "en"


def test_secondary_language_requires_two_detected_blocks():
    doc = doc_with([Node(Role.P, page=1, text=SPANISH), Node(Role.P, page=1, text=SPANISH)])
    assert second_block_language(doc, "en-US") == "es"
    assert second_block_language(doc_with([Node(Role.P, page=1, text=SPANISH)]), "en-US") is None


def test_spanish_text_with_contradicting_lang_is_es_and_reported():
    doc = doc_with([Node(Role.P, page=1, text=SPANISH)], lang="en-US")
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue) == ("es", "language-mismatch")


def test_same_primary_subtag_is_not_a_mismatch():
    doc = doc_with([Node(Role.P, page=1, text=SPANISH)], lang="es-MX")
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue) == ("es-MX", None)


def test_short_text_is_not_detected():
    doc = doc_with([Node(Role.P, page=1, text="Hola a todos")], lang=None)
    assert resolve_language(doc).detected is None
    assert document_language(doc) == ("en", False)


def test_non_latin_text_is_not_detected_as_latin():
    # Mostly Hangul, with a few Spanish function words: the script decides, not the words.
    doc = doc_with([Node(Role.P, page=1, text="안녕하세요 여러분들 모두 오늘 건강검진을 받으세요 el los")],
                   lang=None)
    assert resolve_language(doc).detected is None
    assert document_language(doc) == ("ko", False)


def test_stopword_lists_do_not_overlap():
    words = [w for ws in STOPWORDS.values() for w in ws]
    assert len(words) == len(set(words))


def test_detection_needs_a_clear_margin():
    assert detect_latin_language([SPANISH]) == "es"
    assert detect_latin_language(["the of and to is are"]) is None          # 6 hits, below minimum
    # 8 English hits against 4 Spanish: the winner has less than MARGIN times the runner-up.
    assert detect_latin_language(["the of and to is are with for el los las es"]) is None


def test_mismatch_is_listed_in_review_items():
    doc = doc_with([Node(Role.P, page=1, text=ENGLISH)], lang="es")
    items = review_items(doc)
    assert items[-1]["warn"] == "language-mismatch"
    assert items[-1]["severity"] == "warning" and items[-1]["page"] is None


def test_no_language_issue_means_no_review_item():
    doc = doc_with([Node(Role.P, page=1, text=ENGLISH)], lang="en-US")
    assert review_items(doc) == []


def test_spanish_document_without_lang_renders_as_es(tmp_path):
    index = write(tmp_path, doc_with([Node(Role.P, page=1, text=SPANISH)], lang=None))
    assert '<html lang="es">' in index
    assert 'dir="rtl"' not in index


def test_english_block_in_arabic_document_is_ltr(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text="مرحبا بكم في العيادة الصحية"),
                    Node(Role.H2, page=1, text="Are You at Risk?"),
                    Node(Role.P, page=1, text="2026")], lang="ar")
    index = write(tmp_path, doc)
    assert index.count('dir="ltr"') == 1
    assert "Are You at Risk?" in index.split('dir="ltr"')[1].split("</h2>")[0]


ARABIC = "مرحبا بكم في العيادة الصحية"
# Arabic letters must outnumber the English ones, or the whole document reads as English.
ARABIC_LONG = ("يجب على جميع الأشخاص الذين تتراوح أعمارهم بين ثمانية عشر عاما وخمسة وستين "
               "عاما أن يفحصوا أنفسهم للكشف عن الفيروس في العيادة الصحية المحلية. ") * 2


def test_english_blocks_in_arabic_document_get_their_language(tmp_path):
    # "HEPATITIS B" alone is too short to detect; it reads as English with the sentences.
    doc = doc_with([Node(Role.H2, page=1, text="HEPATITIS B"),
                    Node(Role.P, page=1, text=ARABIC_LONG),
                    Node(Role.P, page=1, text=ENGLISH)], lang="ar")
    index = write(tmp_path, doc)
    assert '<html lang="ar" dir="rtl">' in index
    assert re.search(r'<h2 id="[^"]*" class="caps" dir="ltr" lang="en" data-node', index)
    assert re.search(r'<p dir="ltr" lang="en" data-node="[^"]*">The vaccines', index)
    assert re.search(r'<p data-node="[^"]*">يجب', index)  # Arabic matches the document: no lang
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["ltrLang"] == "en"


def test_left_to_right_document_has_no_per_block_lang(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text=ENGLISH),
                    Node(Role.P, page=1, text=ARABIC)], lang="en-US")
    index = write(tmp_path, doc)
    assert index.count("lang=") == 1  # only <html lang="en-US">
    assert 'dir="rtl"' in index
    assert "ltrLang" not in json.loads((tmp_path / "manifest.json").read_text())


def test_numbers_only_block_in_arabic_document_has_no_lang(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text=ARABIC_LONG),
                    Node(Role.P, page=1, text=ENGLISH),
                    Node(Role.P, page=1, text="2026 - 1234")], lang="ar")
    index = write(tmp_path, doc)
    assert '<html lang="ar" dir="rtl">' in index
    assert re.search(r'<p data-node="[^"]*">2026 - 1234</p>', index)


def test_arabic_document_with_too_little_english_gets_no_lang(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text=ARABIC_LONG),
                    Node(Role.H2, page=1, text="Are You at Risk?")], lang="ar")
    index = write(tmp_path, doc)
    assert '<html lang="ar" dir="rtl">' in index
    assert re.search(r'<h2 id="[^"]*" dir="ltr" data-node', index)
    assert "lang=" not in index.split("<body", 1)[1]
    assert "ltrLang" not in json.loads((tmp_path / "manifest.json").read_text())


def test_english_paragraph_in_spanish_document_gets_lang(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text=SPANISH * 5),
                    Node(Role.P, page=1, text=ENGLISH),
                    Node(Role.H2, page=1, text="Las vacunas")], lang="es-MX")
    index = write(tmp_path, doc)
    assert '<html lang="es-MX">' in index
    assert re.search(r'<p lang="en" data-node="[^"]*">The vaccines', index)
    assert re.search(r'<h2 id="[^"]*" data-node="[^"]*">Las vacunas</h2>', index)


def test_short_spanish_heading_gets_no_language_guess(tmp_path):
    doc = doc_with([Node(Role.P, page=1, text=ENGLISH),
                    Node(Role.H2, page=1, text="Las vacunas")])
    index = write(tmp_path, doc)
    assert re.search(r'<h2 id="[^"]*" data-node="[^"]*">Las vacunas</h2>', index)


@pytest.mark.parametrize("role, tag", [
    (Role.H1, "p"), (Role.H2, "h2"), (Role.P, "p"),
    (Role.CAPTION, "p"), (Role.NOTE, "p"), (Role.LI, "li"),
])
def test_language_switch_is_rendered_on_each_text_block(tmp_path, role, tag):
    block = Node(role, page=1, text=ENGLISH)
    content = Node(Role.LIST, page=1, children=[block]) if role == Role.LI else block
    doc = doc_with([Node(Role.P, page=1, text=SPANISH * 5), content], lang="es-MX")
    index = write(tmp_path, doc)
    assert re.search(fr'<{tag}\b[^>]* lang="en" data-node="{re.escape(block.id)}">', index)


@pytest.mark.parametrize("text, expected", [
    ("This is for your children", "en"),  # four function words in a short block
    ("This is your vaccine", None),
    ("the and el los", None),  # no clear winning language
    ("HEPATITIS B", None),
    ("2026 - 1234", None),
    ("This is for your " + "vaccine " * 36, None),  # long blocks need eight hits
])
def test_block_language_needs_enough_evidence(text, expected):
    assert block_language(text, "es-MX", None) == expected


def test_block_language_compares_primary_subtags_and_preserves_rtl_pool():
    assert block_language(ENGLISH, "EN-us", None) is None
    assert block_language("HEPATITIS B", "ar", "en") == "en"
    assert block_language(ENGLISH, "ar", None) is None
    assert block_language(ARABIC, "ar", "en") is None


TRADITIONAL = ("每位會員的醫療費用與藥局福利說明。這項計劃的保險給付，應從您的免賠額開始計算，"
               "網路內的醫師和醫院可以降低費用。請聯絡我們的客服中心了解詳細資料。")
SIMPLIFIED = ("每位会员的医疗费用与药局福利说明。这项计划的保险给付，应从您的免赔额开始计算，"
              "网络内的医师和医院可以降低费用。请联络我们的客服中心了解详细资料。")


def test_chinese_text_beats_an_english_lang():
    # The Chinese summary of benefits declares /Lang en-US: the text, not the tag, decides.
    doc = doc_with([Node(Role.P, page=1, text=TRADITIONAL),
                    Node(Role.P, page=1, text="Your plan year: January 1 - December 31")])
    choice = resolve_language(doc)
    assert (choice.tag, choice.declared, choice.issue) == ("zh-Hant", "en-US", "language-mismatch")
    assert document_language(doc) == ("zh-Hant", False)


def test_simplified_chinese_is_zh_hans_and_undeclared_chinese_is_read_too():
    assert document_language(doc_with([Node(Role.P, page=1, text=SIMPLIFIED)])) == ("zh-Hans", False)
    choice = resolve_language(doc_with([Node(Role.P, page=1, text=TRADITIONAL)], lang=None))
    assert (choice.tag, choice.issue) == ("zh-Hant", None)  # like Hangul: no review item


def test_a_chinese_lang_is_kept_and_japanese_text_is_not_called_chinese():
    assert resolve_language(doc_with([Node(Role.P, page=1, text=TRADITIONAL)], lang="zh-TW")).issue is None
    japanese = ("医療費の自己負担額は、年間の免責額を超えた分からお支払いいただく必要はありません。"
                "ご不明な点は、会員サービスまでお問い合わせください。")
    assert document_language(doc_with([Node(Role.P, page=1, text=japanese)])) == ("ja", False)


def test_a_few_foreign_words_do_not_override_the_lang():
    # Mostly English with one Chinese heading: the English /Lang stands.
    doc = doc_with([Node(Role.P, page=1, text=ENGLISH), Node(Role.H2, page=1, text="醫療費用")])
    assert (resolve_language(doc).tag, resolve_language(doc).issue) == ("en-US", None)


def test_variant_han_letters_count_toward_the_kana_share():
    # Three kana in a long Chinese block do not make the block Japanese.
    text = TRADITIONAL + "あいう"
    assert document_language(doc_with([Node(Role.P, page=1, text=text)])) == ("zh-Hant", False)


@pytest.mark.parametrize("text, tag", [
    ("주의: 한국어를 사용하는 경우, 언어 지원 서비스가 무료로 제공됩니다.", "ko"),
    ("ΠΡΟΣΟΧΗ: εάν μιλάτε Ελληνικά, μπορείτε να λάβετε δωρεάν υπηρεσίες γλωσσικής βοήθειας.", "el"),
    ("โปรดทราบ: หากคุณพูดภาษาไทย คุณจะสามารถใช้บริการความช่วยเหลือได้โดยไม่มีค่าใช้จ่าย", "th"),
    ("ચેતવણી: જો તમે ગુજરાતી બોલતા હોવ, તો તમારા માટે મફત ભાષા સહાયતા સેવાઓ ઉપલબ્ધ છે.", "gu"),
    ("សូមយកចិត្តទុកដាក់៖ ប្រសិនបើអ្នកនិយាយភាសាខ្មែរ នឹងមានសេវាកម្មជំនួយភាសា", "km"),
    ("注意：日本語を話される場合、無料で言語支援サービスをご利用いただけます。", "ja"),
    (TRADITIONAL, "zh-Hant"),
    (SIMPLIFIED, "zh-Hans"),
])
def test_a_block_in_a_script_that_names_one_language_gets_that_language(text, tag):
    assert block_language(text, "en-US", None) == tag


@pytest.mark.parametrize("text", [
    "ВНИМАНИЕ! Если вы говорите на русском языке, вам бесплатно доступны услуги переводчика.",
    "ध्यान दें: यदि आप हिन्दी बोलते हैं, तो आपके लिए भाषा सहायता सेवाएं उपलब्ध हैं।",
    "ܐܘܪܚܐ ܕܡܠܬܐ ܕܐܠܗܐ ܒܠܫܢܐ ܣܘܪܝܝܐ",
    "α β γ",                      # three Greek letters of a formula are not Greek text
    "Δ",
    "注意",                        # two kanji could be Chinese or Japanese
    "Take 5 μg of folate and 40 mg of iron daily.",
])
def test_a_shared_or_thin_script_gets_no_language(text):
    assert block_language(text, "en-US", None) is None


def test_a_block_in_the_documents_own_script_gets_no_language():
    assert block_language("주의: 한국어를 사용하는 경우, 언어 지원 서비스가 무료로 제공됩니다.",
                          "ko", None) is None
    assert block_language(TRADITIONAL, "zh-TW", None) is None


def test_a_block_that_mixes_two_taglines_is_not_read_as_either():
    text = ("ध्यान दें: यदि आप हिन्दी बोलते हैं, तो आपके लिए भाषा सहायता सेवाएं उपलब्ध हैं। "
            "ATTENZIONE: Se parla italiano, sono disponibili per lei servizi gratuiti di "
            "assistenza linguistica.")
    assert block_language(text, "en-US", None) is None


@pytest.mark.parametrize("text, tag", [
    ("ATENCIÓN: Si usted habla español, tiene a su disposición servicios de asistencia "
     "lingüística sin cargo.", "es"),
    ("ACHTUNG: Wenn Sie Deutsch sprechen, steht Ihnen ein kostenloser Fremdsprachenservice "
     "zur Verfügung.", "de"),
    ("ATENÇÃO: Se você fala português brasileiro, oferecemos serviços gratuitos de "
     "assistência para idiomas.", "pt"),
    ("ATTENZIONE: Se parla italiano, sono disponibili per lei servizi gratuiti.", "it"),
    ("ATTENTION: Si vous parlez français, des services d'aide linguistique vous sont "
     "proposés gratuitement.", "fr"),
])
def test_a_short_latin_tagline_that_names_its_language_gets_it(text, tag):
    assert block_language(text, "en-US", None) == tag


@pytest.mark.parametrize("text", [
    "ATANSYON: Si ou pale Kreyòl Ayisyen, sèvis asistans lang, gratis, disponib pou ou.",
    "Bilagáana bizaad doo bee yáníłti’ dago, saad bee aná’awo’ bee hane’, la sin cargo.",
    "Ask for a Spanish interpreter: español, 1-800-555-0100.",
    "Visit Los Angeles, Las Vegas and El Paso.",
])
def test_a_latin_block_without_clear_evidence_gets_no_language(text):
    assert block_language(text, "en-US", None) is None


SPANISH_PAGE = [
    Node(Role.H2, page=2, text="Notificación de no discriminación"),
    Node(Role.P, page=2, text=SPANISH),
    Node(Role.P, page=2, text="Puede presentar el reclamo por correo, fax o por teléfono. "
                              "Los formularios están en el sitio web."),
    Node(Role.LIST, page=2, children=[
        Node(Role.LI, page=2, text="Intérpretes capacitados."),
        Node(Role.LI, page=2, text="Información escrita en otros idiomas.")]),
]
ENGLISH_PAGE = [Node(Role.H2, page=1, text="Notice of Non-Discrimination"),
                Node(Role.P, page=1, text=ENGLISH)]


def test_a_page_that_reads_as_one_other_language_is_that_language():
    doc = doc_with(ENGLISH_PAGE + SPANISH_PAGE)
    assert page_languages(doc, "en-US") == {2: "es"}


def test_a_page_with_a_block_of_another_language_is_a_mixed_page():
    page = SPANISH_PAGE + [Node(Role.P, page=2, text=ENGLISH)]
    assert page_languages(doc_with(page), "en-US") == {}


def test_a_page_with_a_tagline_in_another_script_is_a_mixed_page():
    korean = Node(Role.P, page=2, text="주의: 한국어를 사용하는 경우, 언어 지원 서비스가 무료로 제공됩니다.")
    assert page_languages(doc_with(SPANISH_PAGE + [korean]), "en-US") == {}


def test_a_page_in_the_documents_language_has_no_page_language():
    assert page_languages(doc_with(ENGLISH_PAGE), "en-US") == {}
    assert page_languages(doc_with(SPANISH_PAGE), "es") == {}


def test_a_block_with_nothing_to_say_takes_the_page_language():
    assert block_language("Notificación de no discriminación", "en-US", None) is None
    assert block_language("Notificación de no discriminación", "en-US", None, "es") == "es"
    # ... unless its own function words say it is something else, or the page is its own.
    assert block_language("Call us if you need help", "en-US", None, "es") is None
    assert block_language("Notificación de no discriminación", "es", None, "es") is None
    # ... and a script that is not Latin keeps to what its letters say.
    assert block_language("ध्यान दें: यदि आप हिन्दी बोलते हैं", "en-US", None, "es") is None


def test_every_block_of_a_spanish_page_is_tagged_and_the_english_page_is_not(tmp_path):
    doc = doc_with(ENGLISH_PAGE + SPANISH_PAGE)
    doc.pages.append(PageInfo(2, 612, 792))
    render_html(doc, "Clinic", tmp_path, page_widths=(800,))
    html = (tmp_path / "index.html").read_text().split("<main", 1)[1]
    english, spanish = html.split('<div class="pg" id="p2"')
    assert "lang=" not in english
    assert len(re.findall(r'<(?:h2|p|li)\b[^>]*lang="es"', spanish)) == 5  # heading, 2 p, 2 li


def test_script_less_documents_and_greek_ones_are_read_by_their_letters():
    greek = "Καλώς ήρθατε στην κλινική μας. Παρακαλούμε καλέστε μας για ραντεβού."
    assert document_language(doc_with([Node(Role.P, page=1, text=greek)], lang=None)) == ("el", False)
    thai = "ยินดีต้อนรับสู่คลินิกของเรา กรุณาโทรเพื่อนัดหมาย"
    assert document_language(doc_with([Node(Role.P, page=1, text=thai)], lang=None)) == ("th", False)


def test_a_page_needs_two_blocks_that_read_as_its_language_alone():
    # A directory's names pool into "Portuguese" through a stray word or two; no block says so.
    names = [Node(Role.P, page=1, text=f"Clinic Row {n} Com Smith")
             for n in range(10)]
    assert page_languages(doc_with(names), "en-US") == {}
    one = SPANISH_PAGE[:2] + [Node(Role.P, page=2, text="Intérpretes capacitados.")]
    assert page_languages(doc_with(one), "en-US") == {}


def test_a_form_number_on_a_one_language_page_gets_no_language():
    assert block_language("Y0137_M41484PY25_C", "en-US", None, "es") is None


# A provider directory: one short entry per provider, each with its web address or email,
# under an English /Lang, with a few English sentences of instructions between them.
DIRECTORY_ENTRY = ("Casa de Salud Clinic, 2200 S Houghton Rd, Tucson. "
                   "www.casadesalud.com, frontdesk@casadesalud.com")
NAMES_ENTRY = "Clinica El Rio, Los Arcos Pharmacy, Las Palmas Medical"


def test_function_words_inside_addresses_and_compounds_are_not_counted():
    # "com" (Portuguese "with") of a domain or an email, "non" (Italian "not") of a compound.
    assert detect_latin_language(["www.casadesalud.com, frontdesk@casadesalud.com"] * 4) is None
    assert detect_latin_language(["Non-Participating, Non-Emergency"] * 4) is None
    # The same words standing alone are still words.
    assert detect_latin_language(["Você pode falar com o seu médico, com a sua família."] * 4) == "pt"


def test_a_directory_of_addresses_keeps_its_declared_english():
    doc = doc_with([Node(Role.P, page=1, text=DIRECTORY_ENTRY) for _ in range(40)]
                   + [Node(Role.P, page=1, text=ENGLISH)], lang="en-US")
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue) == ("en-US", None)


def test_names_alone_do_not_outvote_the_declared_language():
    # The pooled words read as Spanish ("el", "los", "las" of names), but no block reads as
    # Spanish by itself, and the English sentences do: the declared /Lang stands.
    doc = doc_with([Node(Role.P, page=1, text=NAMES_ENTRY) for _ in range(40)]
                   + [Node(Role.P, page=1, text=ENGLISH)] * 2, lang="en-US")
    assert detect_latin_language([NAMES_ENTRY] * 40 + [ENGLISH] * 2) == "es"
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue) == ("en-US", None)


# The text of the robustness harness's tagged base: too few function words to detect (#326).
SHORT_ENGLISH = ["Chapter One", "First paragraph.", "A paragraph that continues here.", "Details",
                 "Name", "Value"]


def short_doc(texts, lang):
    return doc_with([Node(Role.P, page=1, text=t) for t in texts], lang=lang)


def test_a_lang_not_written_in_latin_loses_to_short_latin_text():
    doc = short_doc(SHORT_ENGLISH, "ja-JP")
    choice = resolve_language(doc)
    assert (choice.tag, choice.declared, choice.detected, choice.issue, choice.unsure) == \
        ("en", "ja-JP", None, "language-mismatch", False)
    [item] = [i for i in review_items(doc) if i["warn"] == "language-mismatch"]
    assert item["text"].startswith("The PDF's language is ja-JP; the text is in the Latin script")
    assert "Published as en." in item["text"]


def test_a_lang_not_written_in_latin_loses_to_detected_latin_text():
    choice = resolve_language(doc_with([Node(Role.P, page=1, text=SPANISH)], lang="ko"))
    assert (choice.tag, choice.issue) == ("es", "language-mismatch")


def test_too_little_latin_text_keeps_the_lang_and_flags_it():
    doc = short_doc(["Name", "Value", "Date"], "ja-JP")
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue, choice.unsure) == ("ja-JP", "language-mismatch", True)
    [item] = [i for i in review_items(doc) if i["warn"] == "language-mismatch"]
    assert "too short to say which language" in item["text"] and "Published as ja-JP." in item["text"]


def test_a_script_in_both_maps_is_counted_once():
    # 59 Latin letters and two Arabic ones (3%): Arabic is in SCRIPT_LANG and SCRIPT_LANGS.
    choice = resolve_language(short_doc([*SHORT_ENGLISH, "مر"], "ar"))
    assert (choice.tag, choice.issue) == ("en", "language-mismatch")


def test_a_japanese_document_with_english_in_it_keeps_its_lang():
    # Most letters are Latin, but a third are Japanese: not an English text under a wrong /Lang.
    doc = short_doc(["本書は申請者のための案内です。必要な書類をそろえて提出してください。",
                     *SHORT_ENGLISH], "ja")
    choice = resolve_language(doc)
    assert (choice.tag, choice.issue) == ("ja", None)


def test_short_text_leaning_to_another_latin_language_keeps_the_lang_and_flags_it():
    doc = short_doc(["Scanned letter of the county office", "The clerk filed the deed in March.",
                     "Fees are due within thirty days."], "de-DE")
    choice = resolve_language(doc)
    assert (choice.tag, choice.detected, choice.issue, choice.unsure) == \
        ("de-DE", "en", "language-mismatch", True)
    [item] = [i for i in review_items(doc) if i["warn"] == "language-mismatch"]
    assert "may read as en, but it is too short to be sure" in item["text"]


def test_short_text_in_its_declared_language_is_not_flagged():
    for texts, lang in ((["Solicitud de permiso", "Nombre y apellidos", "Firme el formulario"], "es"),
                        (["Nombre del solicitante", "El formulario es para los padres."], "es-MX"),
                        (SHORT_ENGLISH, "en-US"), (["Name", "Value"], "en")):
        choice = resolve_language(short_doc(texts, lang))
        assert (choice.tag, choice.issue) == (lang, None), texts
