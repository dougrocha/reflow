"""What a text box asks for, from its label (analyze.input_purpose)."""

import pytest

from reflow.analyze.input_purpose import InputPurpose, input_purpose

EMAIL = InputPurpose("email", "email", "email")
TEL = InputPurpose("tel", "tel", "tel")


@pytest.mark.parametrize("label, want", [
    ("Email", EMAIL),
    ("EMAIL ADDRESS", EMAIL),
    ("E-mail address:", EMAIL),
    ("Your email (optional)", EMAIL),
    ("Phone", TEL),
    ("Daytime Telephone", TEL),
    ("Cell phone number", TEL),
    ("Fax", InputPurpose("tel", None, "tel")),
    ("FAX TELEPHONE", InputPurpose("tel", None, "tel")),
    ("ZIP CODE", InputPurpose(None, "postal-code", "numeric")),
    ("ZIP", InputPurpose(None, "postal-code", "numeric")),
    ("Postal code", InputPurpose(None, "postal-code", None)),
    ("City", InputPurpose(None, "address-level2", None)),
    ("STATE", InputPurpose(None, "address-level1", None)),
    ("Street address", InputPurpose(None, "street-address", None)),
    ("Mailing Address", InputPurpose(None, "street-address", None)),
    ("Address (number, street, and apt. or suite no.)", InputPurpose(None, "street-address", None)),
    ("First name", InputPurpose(None, "given-name", None)),
    ("Last Name", InputPurpose(None, "family-name", None)),
    ("Surname", InputPurpose(None, "family-name", None)),
    ("Full name", InputPurpose(None, "name", None)),
    ("Date of birth", InputPurpose(None, "bday", None)),
    ("DOB (MM/DD/YYYY)", InputPurpose(None, "bday", None)),
    ("Date of Birth MM/DD/YYYY", InputPurpose(None, "bday", None)),
    ("Social security number", InputPurpose(None, None, "numeric")),
    ("SSN", InputPurpose(None, None, "numeric")),
    ("NPI", InputPurpose(None, None, "numeric")),
    ("Amount", InputPurpose(None, None, "decimal")),
])
def test_a_label_names_what_the_box_asks_for(label, want):
    assert input_purpose(label) == want


@pytest.mark.parametrize("label", [
    "",
    "Name",                                  # whose name is not clear
    "City, state, and ZIP code",             # three answers in one box
    "Mailing Address of Applicant (Street Address or P.O. Box)",
    "NAME OF APPLICANT (LAST, FIRST, MIDDLE INITIAL), BUSINESS, OR TRUST NAME",
    "Member ID",                             # may hold letters: no digit keypad
    "Account number",
    "Assessor's parcel number",
    "Email the completed form to the address below",
    "Phone call notes",
    "Update",                                # "date" only as a whole word
    "State the reason for your appeal",
    "Date of the hearing at which the appeal board decided to hear the appeal",
    "Teléfono",                              # not English: left out
])
def test_a_label_that_is_not_certain_gets_nothing(label):
    assert input_purpose(label) is None


@pytest.mark.parametrize("label", [
    "Employer email", "Agent phone", "Physician's first name", "Provider ZIP code",
    "Spouse date of birth", "Dependent last name", "Payee city", "Patient address",
    "Authorized representative email", "Emergency contact phone",
])
def test_a_label_about_someone_else_is_not_the_readers_own_data(label):
    got = input_purpose(label)
    # Most of these match no rule at all. Those that do keep no autocomplete token.
    assert got is None or got.autocomplete is None


@pytest.mark.parametrize("heading", [
    "2. CONTACT INFORMATION - AGENT, ATTORNEY, OR RELATIVE OF APPLICANT",
    "Employer information", "Physician", "Spouse", "Dependents", "Payee details",
    "Provider Information", "PCP",
])
def test_a_heading_about_someone_else_withholds_the_token_but_not_the_keyboard(heading):
    assert input_purpose("EMAIL ADDRESS", context=[heading]) == InputPurpose("email", None, "email")
    assert input_purpose("ZIP CODE", context=[heading]) == InputPurpose(None, None, "numeric")
    assert input_purpose("City", context=[heading]) is not None
    assert input_purpose("City", context=[heading]).autocomplete is None


def test_the_readers_own_heading_keeps_the_token():
    for heading in ("1. APPLICANT INFORMATION - PLEASE PRINT", "Your information", "Member"):
        assert input_purpose("EMAIL ADDRESS", context=[heading]) == EMAIL


def test_every_enclosing_heading_counts():
    assert input_purpose("Phone", context=["Part 2", "Physician"]).autocomplete is None


def test_the_tooltip_can_withhold_a_token_the_label_would_give():
    assert input_purpose("Email", tooltip="Agent email address").autocomplete is None


def test_a_tooltip_that_says_something_else_gives_nothing():
    assert input_purpose("Phone", tooltip="Email address") is None


def test_with_no_printed_label_the_tooltip_names_the_box():
    assert input_purpose(None, tooltip="Email address") == EMAIL
    assert input_purpose("", aria="ZIP code") == InputPurpose(None, "postal-code", "numeric")


def test_a_box_for_part_of_a_telephone_number_gets_no_whole_number_token():
    assert input_purpose("Daytime Telephone", width=55.6) == InputPurpose("tel", None, "tel")
    assert input_purpose("Daytime Telephone", width=120) == TEL
    assert input_purpose("Daytime Telephone", aria="Daytime Telephone Area Code", width=22.6) == \
        InputPurpose("tel", "tel-area-code", "numeric")


def test_a_fax_number_never_gets_a_token_and_never_a_date_type():
    assert input_purpose("Fax", width=200) == InputPurpose("tel", None, "tel")
    for label in ("Date of birth", "ZIP", "SSN"):
        got = input_purpose(label)
        assert got.type is None, label
    # A date gets no inputmode either: a numeric keypad (iOS) has no "/" for "10/12/2024".
    assert input_purpose("Date") is None
    assert input_purpose("Date of birth").inputmode is None


def test_a_hint_in_parentheses_still_names_whose_data_it_is():
    assert input_purpose("Email (spouse)").autocomplete is None
    assert input_purpose("Email", tooltip="Email (agent)").autocomplete is None
    assert input_purpose("Email", context=["Contact information (physician)"]).autocomplete is None
    assert input_purpose("Phone", tooltip="Phone (area code)", width=25) == InputPurpose("tel", "tel-area-code", "numeric")


def test_a_numbered_line_of_an_address_gets_no_token():
    assert input_purpose("Address line 1") is None
    assert input_purpose("Address line 2") is None
    assert input_purpose("Mailing address").autocomplete == "street-address"
