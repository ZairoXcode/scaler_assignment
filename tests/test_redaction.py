"""Unit tests for PiiRedactor: entity resolution, consistent replacements, and multi-run splicing."""

import pytest
import docx
from detector import PiiEntity
from redactor import EntityResolver, PiiRedactor


@pytest.fixture
def redactor():
    return PiiRedactor("config.yaml")


# ---------------------------------------------------------------------
# 1. Entity Resolution & Consistency Tests
# ---------------------------------------------------------------------
def test_entity_resolution_variants():
    resolver = EntityResolver()
    id1 = resolver.resolve("Kushal Subbayya Hegde", "PERSON")
    id2 = resolver.resolve("Kushal Hegde", "PERSON")
    id3 = resolver.resolve("Mr. Kushal Subbayya Hegde", "PERSON")
    assert id1 == id2
    assert id1 == id3


def test_consistent_fake_replacement(redactor):
    """Different alias mentions receive identical synthetic replacement."""
    e1 = PiiEntity("Kushal Subbayya Hegde", "PERSON", 0, 21, 0.9, "spacy_ner")
    e2 = PiiEntity("Kushal Hegde", "PERSON", 0, 12, 0.9, "spacy_ner")
    fake1 = redactor.get_replacement(e1)
    fake2 = redactor.get_replacement(e2)
    assert fake1 == fake2
    assert len(fake1) > 0


def test_trust_replacement(redactor):
    e = PiiEntity("Dhaulagiri Family Trust", "ORGANIZATION", 0, 23, 0.9, "spacy_ner", tier=1)
    fake = redactor.get_replacement(e)
    assert "Family Trust" in fake
    assert fake != "Dhaulagiri Family Trust"


def test_cross_entity_email(redactor):
    e = PiiEntity("sarthak.malvadkar@kshinternational.com", "EMAIL", 0, 38, 1.0, "regex_email")
    fake = redactor.get_replacement(e)
    assert "@apexzenith.com" in fake


# ---------------------------------------------------------------------
# 2. Multi-Run Splicing & Formatting Preservation Tests
# ---------------------------------------------------------------------
def test_single_run_splicing(redactor):
    """Format preservation inside a single run."""
    doc = docx.Document()
    p = doc.add_paragraph()
    r = p.add_run("Contact person is Sarthak Malvadkar for queries.")
    r.bold = True

    start = p.text.find("Sarthak Malvadkar")
    end = start + len("Sarthak Malvadkar")
    entity = PiiEntity("Sarthak Malvadkar", "PERSON", start, end, 0.9, "spacy_ner")

    audits = redactor.redact_paragraph(p, [entity], "TestPara 0")
    assert len(audits) == 1
    assert "Sarthak Malvadkar" not in p.text
    assert "Contact person is " in p.text
    assert p.runs[0].bold is True


def test_multi_run_splicing_across_boundaries(redactor):
    """Crucial edge case: entity split across multiple run boundaries."""
    doc = docx.Document()
    p = doc.add_paragraph()
    r1 = p.add_run("The promoter is ")
    r2 = p.add_run("Kushal ")
    r2.bold = True
    r3 = p.add_run("Subbayya ")
    r3.italic = True
    r4 = p.add_run("Hegde")
    r4.bold = True
    r5 = p.add_run(", who holds equity.")

    full = p.text
    start = full.find("Kushal Subbayya Hegde")
    end = start + len("Kushal Subbayya Hegde")
    entity = PiiEntity("Kushal Subbayya Hegde", "PERSON", start, end, 0.9, "spacy_ner")

    audits = redactor.redact_paragraph(p, [entity], "TestPara 1")
    assert len(audits) == 1
    assert "Kushal Subbayya Hegde" not in p.text
    assert "The promoter is " in p.text
    assert ", who holds equity." in p.text

    # Prefix formatting preserved
    assert r1.text == "The promoter is "
    # Suffix formatting preserved
    assert r5.text == ", who holds equity."


def test_repeated_pii_occurrence_in_paragraph(redactor):
    """Multiple occurrences of the same PII in a single paragraph are all replaced consistently."""
    doc = docx.Document()
    p = doc.add_paragraph("Kushal Hegde discussed the plan with Kushal Hegde.")
    e1 = PiiEntity("Kushal Hegde", "PERSON", 0, 12, 0.9, "spacy_ner")
    e2 = PiiEntity("Kushal Hegde", "PERSON", 32, 44, 0.9, "spacy_ner")

    audits = redactor.redact_paragraph(p, [e1, e2], "TestPara 2")
    assert len(audits) == 2
    assert "Kushal Hegde" not in p.text
    # Both occurrences must use the same synthetic replacement
    assert audits[0].replacement == audits[1].replacement


def test_common_word_occurrence_verification(redactor):
    """A common word detected in one paragraph and redacted must not flag unredacted occurrences in other paragraphs."""
    doc = docx.Document()
    p1 = doc.add_paragraph("Approval by Directors.")
    p2 = doc.add_paragraph("General duties of directors in corporate law.")

    # Only p1 had 'Directors' flagged as PII
    e = PiiEntity("Directors", "PERSON", 12, 21, 0.85, "spacy_ner")
    audits = redactor.redact_paragraph(p1, [e], "Para 1")
    assert len(audits) == 1
    assert "Directors" not in p1.text
    # p2 remains untouched
    assert "directors" in p2.text


def test_distinct_organizations_get_distinct_replacements(redactor):
    """Different organizations (e.g. KSH International vs Waterloo) receive distinct synthetic names."""
    e1 = PiiEntity("KSH International Limited", "ORGANIZATION", 0, 25, 1.0, "tier_1_explicit", tier=1)
    e2 = PiiEntity("WATERLOO INDUSTRIAL PARK VI PRIVATE LIMITED", "ORGANIZATION", 0, 43, 1.0, "spacy_ner", tier=1)
    fake1 = redactor.get_replacement(e1)
    fake2 = redactor.get_replacement(e2)
    assert fake1 != fake2, f"Expected distinct names but both got {fake1!r}"
    assert len(fake1) > 0 and len(fake2) > 0


def test_address_replacement_no_suffix_duplication(redactor):
    """When the paragraph has 'Maharashtra, India' after the PIN, redaction must not duplicate the suffix."""
    doc = docx.Document()
    text = "Registered Office: 11/3, Village Birdewadi, Chakan, Pune – 410 501, Maharashtra, India."
    p = doc.add_paragraph(text)
    addr_text = "11/3, Village Birdewadi, Chakan, Pune – 410 501"
    start = text.find(addr_text)
    end = start + len(addr_text)
    entity = PiiEntity(addr_text, "ADDRESS", start, end, 0.9, "regex_address")
    redactor.redact_paragraph(p, [entity], "TestPara Addr")
    assert "Maharashtra, India, Maharashtra, India" not in p.text
    assert p.text.count("Maharashtra, India") == 1


def test_email_username_does_not_survive_redaction(redactor):
    """The original email username (whether person name or corporate prefix) must never survive redaction."""
    e1 = PiiEntity("Sarthak.malvadkar@kshinterantional.com", "EMAIL", 0, 39, 1.0, "regex_email")
    e2 = PiiEntity("ksh.ipo@nuvama.com", "EMAIL", 0, 18, 1.0, "regex_email")

    fake1 = redactor.get_replacement(e1)
    fake2 = redactor.get_replacement(e2)

    assert "@apexzenith.com" in fake1
    assert "@apexzenith.com" in fake2
    assert "sarthak" not in fake1.lower()
    assert "malvadkar" not in fake1.lower()
    assert "ksh" not in fake2.lower()
    assert "ipo" not in fake2.lower()


def test_same_organization_variants_and_different_organizations(redactor):
    """Organization variants resolve to identical replacement while distinct organizations receive distinct names."""
    e_var1 = PiiEntity("KSH International Limited", "ORGANIZATION", 0, 25, 1.0, "tier_1_explicit", tier=1)
    e_var2 = PiiEntity("KSH International Ltd", "ORGANIZATION", 0, 21, 1.0, "tier_1_explicit", tier=1)
    e_other = PiiEntity("WATERLOO INDUSTRIAL PARK VI PRIVATE LIMITED", "ORGANIZATION", 0, 43, 1.0, "spacy_ner", tier=1)

    fake_var1 = redactor.get_replacement(e_var1)
    fake_var2 = redactor.get_replacement(e_var2)
    fake_other = redactor.get_replacement(e_other)

    assert fake_var1 == fake_var2
    assert fake_var1 != fake_other


def test_multi_run_address_replacement_preserves_suffix(redactor):
    """Multi-run address replacement cleanly splices across boundaries and preserves existing text suffix."""
    doc = docx.Document()
    p = doc.add_paragraph()
    r1 = p.add_run("Registered Office: ")
    r2 = p.add_run("11/3, 11/4 and 11/5, Village Birdewadi, ")
    r2.bold = True
    r3 = p.add_run("Chakan Taluka - Khed, Pune – 410 501")
    r4 = p.add_run(", Maharashtra, India;")

    full_text = p.text
    addr_text = "11/3, 11/4 and 11/5, Village Birdewadi, Chakan Taluka - Khed, Pune – 410 501"
    start = full_text.find(addr_text)
    end = start + len(addr_text)

    entity = PiiEntity(addr_text, "ADDRESS", start, end, 0.9, "regex_address")
    audits = redactor.redact_paragraph(p, [entity], "TestPara MultiRunAddr")

    assert len(audits) == 1
    assert "11/3, 11/4" not in p.text
    assert "Registered Office: " in p.text
    assert ", Maharashtra, India;" in p.text
    assert "Maharashtra, India, Maharashtra, India" not in p.text


