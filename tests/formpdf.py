"""A synthetic AcroForm PDF built with pikepdf, for the form tests."""

from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

LETTER = (612, 792)


def make_form_pdf(path: Path, password: bool = False) -> Path:
    """One page of printed labels and AcroForm fields (top-left coordinates):
    "First name" (text, /TU, /MaxLen on the field) and "Notes" (multiline) with their labels
    above them, a "State" combo with options, a radio group "Contact" of two kids printed as
    "Email" and "Phone" under the question "Preferred contact", and a lone checkbox "Agree"
    printed as "I agree to the terms". With `password`, a text field "Password" holding the
    value "hunter2" is added below them."""
    pdf = pikepdf.new()
    helv = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding))
    page = pdf.add_blank_page(page_size=LETTER)
    page.Resources = Dictionary(Font=Dictionary(Helv=helv))
    height = LETTER[1]

    def printed(x, top, text):
        return f"BT /Helv 10 Tf {x} {height - top - 10} Td ({text}) Tj ET"

    lines = [
        printed(72, 60, "Applicant details"),
        printed(72, 90, "First name"), printed(72, 150, "Notes"), printed(320, 90, "State"),
        printed(72, 230, "Preferred contact"), printed(96, 250, "Email"), printed(96, 270, "Phone"),
        printed(96, 300, "I agree to the terms"),
    ]
    if password:
        lines.append(printed(72, 330, "Password"))
    page.Contents = pdf.make_stream("\n".join(lines).encode())

    def rect(x0, top, x1, bottom):
        return [x0, height - bottom, x1, height - top]

    def appearance(width, h):
        return pdf.make_stream(b"", Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, width, h])

    def widget(r, **kw):
        w = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, F=4, Rect=r, **kw))
        if "/Annots" not in page.obj:
            page.obj.Annots = Array()
        page.obj.Annots.append(w)
        return w

    da = String("/Helv 0 Tf 0 g")
    first = widget(rect(72, 104, 292, 122), FT=Name.Tx, T=String("first"),
                   TU=String("Applicant first name"), Ff=0, DA=da, MaxLen=20, V=String(""),
                   AP=Dictionary(N=appearance(220, 18)))
    notes = widget(rect(72, 164, 292, 214), FT=Name.Tx, T=String("notes"), Ff=1 << 12, DA=da,
                   AP=Dictionary(N=appearance(220, 50)))
    state = widget(rect(320, 104, 420, 122), FT=Name.Ch, T=String("state"), Ff=1 << 17,
                   Opt=Array([String("AZ"), String("CA"), String("NY")]), V=String("CA"),
                   DA=da, AP=Dictionary(N=appearance(100, 18)))
    group = pdf.make_indirect(Dictionary(FT=Name.Btn, T=String("Contact"),
                                         Ff=(1 << 15) | (1 << 14), V=Name("/email"), Kids=Array()))
    for export, top in (("email", 250), ("phone", 270)):
        kid = widget(rect(76, top, 88, top + 12), Parent=group,
                     AS=Name("/email" if export == "email" else "/Off"),
                     MK=Dictionary(CA=String("l")),
                     AP=Dictionary(N=Dictionary({"/" + export: appearance(12, 12),
                                                 "/Off": appearance(12, 12)})))
        group.Kids.append(kid)
    agree = widget(rect(76, 298, 88, 310), FT=Name.Btn, T=String("agree"), V=Name("/Off"),
                   AS=Name("/Off"), MK=Dictionary(CA=String("4")),
                   AP=Dictionary(N=Dictionary({"/Yes": appearance(12, 12),
                                               "/Off": appearance(12, 12)})))
    fields = [first, notes, state, group, agree]
    if password:
        fields.append(widget(rect(72, 344, 292, 362), FT=Name.Tx, T=String("pw"), Ff=1 << 13,
                             DA=da, V=String("hunter2"), AP=Dictionary(N=appearance(220, 18))))
    pdf.Root.AcroForm = Dictionary(Fields=Array(fields),
                                   DR=Dictionary(Font=Dictionary(Helv=helv)), DA=da)
    pdf.save(path)
    return path
