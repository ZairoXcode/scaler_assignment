"""Unit tests for PiiDetector: regex patterns, spaCy NER, 3-tier ORG policy, and suppression.

Each test exercises a specific real-world PII pattern taken from the KSH International RHP
or from realistic financial-document strings. All test inputs were verified directly against
the compiled regex before writing these assertions.
"""

import pytest
from detector import PiiDetector, PiiEntity, luhn_checksum


@pytest.fixture(scope="module")
def detector():
    """Single detector instance shared across the module (model load is slow)."""
    return PiiDetector("config.yaml")


# =====================================================================
# 1. Structured PII – Regex patterns
# =====================================================================

def test_detect_emails(detector):
    """Corporate and sub-domain emails from the RHP are detected correctly."""
    text = "Contact cs.connect@kshinternational.com or prakash.boricha@nuvama.com."
    ents = [e for e in detector.detect(text) if e.category == "EMAIL"]
    texts = [e.text for e in ents]
    assert "cs.connect@kshinternational.com" in texts
    assert "prakash.boricha@nuvama.com" in texts
    assert len(ents) == 2


def test_detect_indian_phones(detector):
    """Landline STD, landline with +91 ISD prefix, and 10-digit mobile are all detected."""
    text = "Tel: +91 20 4505 3237, landline 022-68052182, or mobile +91 9822012345, standalone 9822012345."
    ents = [e for e in detector.detect(text) if e.category == "PHONE"]
    phone_texts = [p.text for p in ents]
    assert "+91 20 4505 3237" in phone_texts
    assert "022-68052182" in phone_texts
    assert "+91 9822012345" in phone_texts
    assert "9822012345" in phone_texts


def test_pan_preserved_as_non_pii(detector):
    """Indian PAN is recognized as a document identifier and NOT flagged as PII."""
    text = "Promoter PAN is ABCDE1234F registered with tax authority."
    ents = detector.detect(text)
    assert not any(e.category == "PAN" for e in ents)
    assert not any("ABCDE1234F" in e.text for e in ents)


def test_detect_ssn(detector):
    """US SSN with valid area/group/serial fields is detected; invalid combos are not."""
    text_valid = "US tax ID: 123-45-6789."
    ents = [e for e in detector.detect(text_valid) if e.category == "SSN"]
    assert len(ents) == 1
    assert ents[0].text == "123-45-6789"

    # Invalid SSNs (reserved area codes) must not be flagged
    for invalid in ["000-45-6789", "666-45-6789", "900-45-6789"]:
        ents_invalid = [e for e in detector.detect(invalid) if e.category == "SSN"]
        assert len(ents_invalid) == 0, f"Invalid SSN flagged: {invalid}"


def test_valid_and_invalid_luhn_credit_card(detector):
    """Luhn-valid card numbers are detected; bad-checksum sequences are rejected.

    Note: Luhn validates mod-10 checksum integrity only. A passing number is not
    proof of a legitimately issued card.
    """
    valid_card = "4532015112830366"
    invalid_card = "4532015112830367"

    # Unit test: checksum function
    assert luhn_checksum(valid_card) is True
    assert luhn_checksum(invalid_card) is False

    # Integration: valid card found, invalid card not found
    valid_ents = [e for e in detector.detect(f"Charged to card {valid_card} today.") if e.category == "CREDIT_CARD"]
    assert len(valid_ents) == 1
    assert valid_ents[0].text == valid_card

    invalid_ents = [e for e in detector.detect(f"Reference sequence {invalid_card} recorded.") if e.category == "CREDIT_CARD"]
    assert len(invalid_ents) == 0


def test_detect_ipv4(detector):
    """Valid IPv4 address embedded in prose is detected; 0.0.0.0 is filtered."""
    text = "Gateway server at 192.168.1.10."
    ents = [e for e in detector.detect(text) if e.category == "IP_ADDRESS"]
    assert len(ents) == 1
    assert ents[0].text == "192.168.1.10"

    # 0.0.0.0 is a placeholder, not a real address
    ents_zero = [e for e in detector.detect("Bind to 0.0.0.0 for all interfaces.") if e.category == "IP_ADDRESS"]
    assert len(ents_zero) == 0


def test_detect_ipv6(detector):
    """Full 8-group IPv6 address is detected. Compressed notation is a known limitation."""
    text = "Server IPv6: 2001:0db8:85a3:0000:0000:8a2e:0370:7334."
    ents = [e for e in detector.detect(text) if e.category == "IP_ADDRESS"]
    assert len(ents) == 1
    assert ents[0].text == "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
    # Compressed form (e.g. 2001:db8::1) is NOT detected - documented limitation.


def test_detect_dob(detector):
    """DOB is extracted only when a birth-context keyword precedes the date."""
    text_dob = "Rohit Kushal Hegde, Date of Birth: August 14, 1982, aged 42."
    ents = [e for e in detector.detect(text_dob) if e.category == "DOB"]
    assert len(ents) == 1
    assert ents[0].text == "August 14, 1982"


def test_financial_dates_not_flagged_as_dob(detector):
    """Financial period dates without a birth-context keyword are never treated as DOB."""
    texts = [
        "For the fiscal year ended March 31, 2024 and June 30, 2023.",
        "quarter ended September 30, 2023",
        "year ended March 31, 2024",
    ]
    for t in texts:
        ents = [e for e in detector.detect(t) if e.category == "DOB"]
        assert len(ents) == 0, f"Falsely detected DOB in: {t!r}"


def test_detect_address_cue(detector):
    """Addresses introduced by 'residing at' are captured with their PIN code."""
    text = "residing at Deccan Gymkhana, Prabhat Road, Pune 411004."
    ents = [e for e in detector.detect(text) if e.category == "ADDRESS"]
    assert len(ents) == 1
    assert "Deccan Gymkhana" in ents[0].text
    assert "411004" in ents[0].text


def test_detect_registrar_address_with_dots(detector):
    """Address following 'Registrar:' cue is detected even when it contains abbreviation dots (L.B.S.)."""
    text = (
        "Registrar: Link Intime India Private Limited, C-101, 247 Park, "
        "L.B.S. Marg, Vikhroli (West), Mumbai 400 083. Tel: +91 22 4918 6200."
    )
    ents = [e for e in detector.detect(text) if e.category == "ADDRESS"]
    assert len(ents) == 1
    assert "L.B.S. Marg" in ents[0].text
    assert "400 083" in ents[0].text


def test_urls_preserved_as_non_pii(detector):
    """Company and regulatory URLs are preserved and NOT flagged as PII."""
    text = "Visit https://www.kshinternational.com or www.sebi.gov.in."
    ents = detector.detect(text)
    assert not any(e.category == "URL" for e in ents)
    assert len(ents) == 0


def test_known_person(detector):
    """Promoter and key-person roster (hard-coded) reliably detects all named individuals."""
    text = "Kushal Subbayya Hegde, Pushpa Kushal Hegde, and Sarthak Malvadkar attended."
    ents = [e for e in detector.detect(text) if e.category == "PERSON"]
    names = [e.text for e in ents]
    assert "Kushal Subbayya Hegde" in names
    assert "Pushpa Kushal Hegde" in names
    assert "Sarthak Malvadkar" in names


# =====================================================================
# 2. 3-Tier Organization Policy
# =====================================================================

def test_tier_1_organization_classification_and_detection(detector):
    """Tier 1 orgs (issuer, corporate promoters, family trusts) are classified and detected."""
    assert detector.classify_organization("KSH International Limited") == 1
    assert detector.classify_organization("Dhaulagiri Family Trust") == 1
    assert detector.classify_organization("Everest Family Trust") == 1
    assert detector.classify_organization("Waterloo Industrial Park VI Private Limited") == 1
    # Any unknown Family Trust name must also be Tier 1 (matched by trust_patterns)
    assert detector.classify_organization("Himalayan Family Trust") == 1

    text = "KSH International Limited and Dhaulagiri Family Trust are promoters."
    ents = [e for e in detector.detect(text) if e.category == "ORGANIZATION"]
    assert len(ents) >= 1
    assert any(e.tier == 1 for e in ents)


def test_tier_2_organization_preserved_by_default(detector):
    """Tier 2 commercial intermediaries are classified 2 and NOT redacted under default policy."""
    assert detector.classify_organization("Nuvama Wealth Management Limited") == 2
    assert detector.classify_organization("Link Intime India Private Limited") == 2

    text = "Book Running Lead Manager is Nuvama Wealth Management Limited."
    ents = [e for e in detector.detect(text) if e.category == "ORGANIZATION"]
    assert len(ents) == 0  # suppressed because redact_tier_2 defaults to False


def test_tier_3_regulatory_bodies_never_redacted(detector):
    """Tier 3 regulatory and statutory bodies are classified 3 and always suppressed."""
    assert detector.classify_organization("Securities and Exchange Board of India") == 3
    assert detector.classify_organization("SEBI") == 3
    assert detector.classify_organization("BSE Limited") == 3
    assert detector.classify_organization("Reserve Bank of India") == 3

    text = "Filed with Securities and Exchange Board of India and listed on BSE Limited."
    ents = [e for e in detector.detect(text) if e.category == "ORGANIZATION"]
    assert len(ents) == 0


# =====================================================================
# 3. Suppression Rules
# =====================================================================

def test_din_suppression(detector):
    """Director Identification Numbers (8 digits prefixed by 'DIN:') are not flagged as PHONE."""
    text = "Amod Joshi, Independent Director, holding DIN: 00135070, Pune"
    ents = [e for e in detector.detect(text) if e.category == "PHONE"]
    assert len(ents) == 0


def test_cin_suppression(detector):
    """CIN codes (L/U + 5 digits + 2 letters + 4 digits + 3 letters + 6 digits) are suppressed."""
    text = "Corporate Identity Number: U28112PN2013PLC146604 registered with RoC."
    ents = detector.detect(text)
    cin_ents = [e for e in ents if "U28112PN2013PLC146604" in e.text]
    assert len(cin_ents) == 0


def test_statutory_acts_suppression(detector):
    """Statutory act citations (Companies Act, SEBI ICDR Regulations) do not produce ORG or PERSON entities."""
    text = "Offered under Section 26 of Companies Act, 2013 and SEBI ICDR Regulations."
    ents = [e for e in detector.detect(text) if e.category == "ORGANIZATION"]
    assert len(ents) == 0


# =====================================================================
# 4. Overlap Resolution
# =====================================================================

def test_overlap_resolution_email_wins(detector):
    """An EMAIL span wins over any lower-priority entity that overlaps it; no duplicate spans."""
    text = "Contact cs.connect@kshinternational.com regarding company affairs."
    ents = detector.detect(text)
    emails = [e for e in ents if e.category == "EMAIL"]
    assert len(emails) == 1
    assert emails[0].text == "cs.connect@kshinternational.com"

    # No two entities may overlap
    sorted_ents = sorted(ents, key=lambda e: e.start_char)
    for i in range(len(sorted_ents) - 1):
        assert sorted_ents[i].end_char <= sorted_ents[i + 1].start_char, (
            f"Overlap: {sorted_ents[i]} and {sorted_ents[i+1]}"
        )


def test_overlap_resolution_higher_priority_wins(detector):
    """When a PERSON name overlaps a lower-priority span, the higher-priority entity is kept."""
    # Promoter name overlap with spaCy ORG: PERSON (75) > ORGANIZATION (70) -> PERSON wins
    text = "Sarthak Malvadkar leads the team."
    ents = detector.detect(text)
    persons = [e for e in ents if e.category == "PERSON" and "Sarthak" in e.text]
    assert len(persons) >= 1


# =====================================================================
# 5. False Positives – ordinary numbers must not be PHONE or CREDIT_CARD
# =====================================================================

def test_ordinary_numbers_not_phone_or_card(detector):
    """Financial reference IDs, order numbers, folio/policy/serial numbers are never PHONE or CREDIT_CARD."""
    cases = [
        "Order reference 12345678 and account 98765432 confirmed.",
        "Invoice # 9876543210 submitted for processing.",
        "Ref No. 87654321 dated yesterday.",
        "Purchase Order PO-87654321 approved.",
        "Total revenue of Rs. 4,200,500 Lakhs recorded.",
        "Tracking ID 1234567890123456 not a card.",
        "Policy No. 9876543210 issued under scheme.",
        "Folio No. 1234567890 in the shareholder register.",
        "Serial No. 9876543210 stamped on the certificate.",
        "Cheque No. 987654 drawn on HDFC Bank.",
        "4532015112830367 is a rejected sequence.",     # bad Luhn -> not CREDIT_CARD
    ]
    for t in cases:
        ents = [e for e in detector.detect(t) if e.category in ("PHONE", "CREDIT_CARD")]
        assert len(ents) == 0, (
            f"False positive in {t!r}: {[(e.text, e.category) for e in ents]}"
        )


def test_dated_month_not_flagged_as_person(detector):
    """Phrases like 'director dated August 16, 2011' must not classify 'dated August' as PERSON."""
    text = "Confirmed by an order of the regional director dated August 16, 2011."
    ents = [e for e in detector.detect(text) if e.category == "PERSON"]
    texts = [e.text.lower() for e in ents]
    assert not any("dated" in t or "august" in t for t in texts), f"False person detected: {texts}"


def test_address_with_and_without_contextual_prefix(detector):
    """Addresses with 'Registered Office: ...' and 'registered office at ...' normalize consistently without 'at '."""
    t1 = "Registered Office: 11/3, 11/4 and 11/5, Village Birdewadi, Chakan Taluka - Khed, Pune – 410 501, Maharashtra, India;"
    t2 = "having its registered office at 11/3, 11/4 and 11/5, Village Birdewadi, Chakan Taluka - Khed, Pune – 410 501, Maharashtra, India;"
    e1 = [e for e in detector.detect(t1) if e.category == "ADDRESS"]
    e2 = [e for e in detector.detect(t2) if e.category == "ADDRESS"]
    assert len(e1) == 1
    assert len(e2) == 1
    assert not e1[0].text.lower().startswith("at ")
    assert not e2[0].text.lower().startswith("at ")
    assert e1[0].text == e2[0].text


def test_repeated_person_and_org_detection(detector):
    """Multiple occurrences of the same person and organization in a single text are all detected."""
    text = "Mr. Kushal Hegde met Mr. Kushal Hegde at KSH International Limited and KSH International Limited."
    ents = detector.detect(text)
    person_ents = [e for e in ents if e.category == "PERSON" and "Kushal" in e.text]
    org_ents = [e for e in ents if e.category == "ORGANIZATION" and "KSH" in e.text]
    assert len(person_ents) == 2
    assert len(org_ents) == 2


def test_numeric_identifiers_untouched(detector):
    """DIN, CIN, Order, and Reference numbers are not flagged as phone or credit card."""
    text = "Director identification DIN 00234567, corporate CIN L27100MH1979PLC021586, and Ref 98765432."
    ents = [e for e in detector.detect(text) if e.category in ("PHONE", "CREDIT_CARD")]
    assert len(ents) == 0


def test_missed_person_names_detected(detector):
    """Key personnel and promoters mentioned in RHP are reliably detected."""
    names = ["Lokesh Shah", "Soumavo Sarkar", "Dinesh Hirachand Munot", "Kushal Hegde"]
    for name in names:
        text = f"The statement was signed by {name} as authorised signatory."
        ents = [e for e in detector.detect(text) if e.category == "PERSON"]
        detected_names = [e.text for e in ents]
        assert name in detected_names, f"Failed to detect {name} in {text!r}"


def test_generic_corporate_terms_not_person(detector):
    """Generic corporate roles, section titles, and email labels are never classified as PERSON."""
    generic_terms = [
        "Shareholders",
        "Promoters",
        "Directors",
        "Key Managerial Personnel",
        "Promoter Selling Shareholders",
        "Selling Shareholders",
        "E-mail",
    ]
    for term in generic_terms:
        text = f"Report presented to {term} of the Company."
        ents = [e for e in detector.detect(text) if e.category == "PERSON" and e.text.lower() == term.lower()]
        assert len(ents) == 0, f"Falsely detected PERSON for {term!r}: {ents}"


def test_phone_with_space_after_plus(detector):
    """Phone numbers with space after + ISD code are detected."""
    text = "Please reach out at + 91 20 45053237 or Tel: + 91 20 45053237."
    ents = [e for e in detector.detect(text) if e.category == "PHONE"]
    assert len(ents) >= 1
    assert any("+ 91 20 45053237" in e.text for e in ents)


def test_email_wins_over_person_span(detector):
    """Email local-part names (e.g. sarthak.malvadkar) or cues do not create competing PERSON entities."""
    text = "Send queries to E-mail: sarthak.malvadkar@kshinternational.com or ksh.ipo@nuvama.com."
    ents = detector.detect(text)
    emails = [e for e in ents if e.category == "EMAIL"]
    persons = [e for e in ents if e.category == "PERSON"]
    assert len(emails) == 2
    # No PERSON entity should overlap or match inside the emails or on 'E-mail'
    for p in persons:
        assert "sarthak" not in p.text.lower()
        assert "malvadkar" not in p.text.lower()
        assert "e-mail" not in p.text.lower()


def test_registered_office_address_with_and_without_prefix(detector):
    """Registered office address is detected both with and without contextual prefix."""
    # With contextual prefix
    t1 = "Registered Office: 11/3, 11/4 and 11/5, Village Birdewadi, Chakan Taluka - Khed, Pune – 410 501, Maharashtra, India;"
    e1 = [e for e in detector.detect(t1) if e.category == "ADDRESS"]
    assert len(e1) == 1
    assert "410 501" in e1[0].text
    assert "Birdewadi" in e1[0].text

    # Without contextual prefix (as in table header cells)
    t2 = "11/3, 11/4 and 11/5 Village Birdewadi Chakan Taluka - Khed Pune – 410 501\nMaharashtra, India"
    e2 = [e for e in detector.detect(t2) if e.category == "ADDRESS"]
    assert len(e2) == 1
    assert "410 501" in e2[0].text
    assert "Birdewadi" in e2[0].text

    # With facility cue
    t3 = "Our manufacturing facility located at 11/3, 11/4 and 11/5, Village Birdewadi, Chakan Taluka - Khed, Pune – 410 501, Maharashtra, India"
    e3 = [e for e in detector.detect(t3) if e.category == "ADDRESS"]
    assert len(e3) == 1
    assert "410 501" in e3[0].text


def test_email_with_person_name_and_company_identifier(detector):
    """Emails containing personal names or company identifiers are completely detected as EMAIL spans."""
    text = "Emails: Sarthak.malvadkar@kshinterantional.com and ksh.ipo@nuvama.com."
    ents = detector.detect(text)
    emails = [e for e in ents if e.category == "EMAIL"]
    email_texts = [e.text for e in emails]
    assert "Sarthak.malvadkar@kshinterantional.com" in email_texts
    assert "ksh.ipo@nuvama.com" in email_texts
    # Neither should have sub-spans detected as PERSON or ORGANIZATION
    other_ents = [e for e in ents if e.category in ("PERSON", "ORGANIZATION")]
    assert len(other_ents) == 0


def test_all_non_pii_identifiers_preserved(detector):
    """PAN, URL, CIN, DIN, and regulatory bodies are strictly preserved and not detected as PII."""
    text = (
        "Entity CIN: U28113PN1989PLC051054, DIN: 00135070, PAN: ABCDE1234F, "
        "Website: www.kshinternational.com, regulator: SEBI, exchange: BSE."
    )
    ents = detector.detect(text)
    assert len(ents) == 0



