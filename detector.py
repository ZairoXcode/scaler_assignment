"""Hybrid PII detection engine for DOCX text.

Detection strategy:
- Regex: structured PII covering email, phone, SSN, credit cards,
  IP addresses, DOB, and physical address patterns.
- spaCy NER: PERSON and ORGANIZATION detection.
- Contextual rules: suppress known non-PII such as DIN/CIN, statutory
  references, regulatory organizations, generic numeric identifiers,
  and document identifiers / web URLs (PAN, URLs).
- Configurable organization policy: Tier 1 / Tier 2 / Tier 3.

The detector returns character-level spans so the redactor can safely
replace entities even when Word splits text across multiple runs.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import re

import spacy
import yaml


@dataclass(frozen=True)
class PiiEntity:
    """A detected PII entity with its exact character span."""

    text: str
    category: str
    start_char: int
    end_char: int
    confidence: float
    detector: str
    tier: Optional[int] = None


def luhn_checksum(card_number: str) -> bool:
    """Return True when a 13-19 digit number passes the Luhn checksum."""

    digits = [int(c) for c in card_number if c.isdigit()]

    if not 13 <= len(digits) <= 19:
        return False

    checksum = 0

    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit

    return checksum % 10 == 0


class PiiDetector:
    """Detect structured and unstructured PII from document text."""

    REGULATORY_DOMAINS = {
        "sebi.gov.in",
        "bseindia.com",
        "nseindia.com",
        "mca.gov.in",
        "rbi.org.in",
        "incometaxindia.gov.in",
        "cdslindia.com",
        "nsdl.co.in",
    }

    STATUTORY_ACT_PATTERNS = [
        re.compile(r"\bCompanies\s+Act(?:,\s*\d{4})?\b", re.I),
        re.compile(
            r"\bSEBI\s+(?:\([^)]+\)\s+)?"
            r"(?:ICDR|LODR|SAST|PIT)?\s*"
            r"(?:Regulations|Guidelines)\b",
            re.I,
        ),
        re.compile(
            r"\bSecurities\s+Contracts?\s+\(Regulation\)\s+Act\b",
            re.I,
        ),
        re.compile(r"\bIncome[\s-]+Tax\s+Act(?:,\s*\d{4})?\b", re.I),
        re.compile(
            r"\b(?:Foreign\s+Exchange\s+Management\s+Act|FEMA)\b",
            re.I,
        ),
        re.compile(
            r"\b(?:Indian\s+Contract\s+Act|"
            r"Insolvency\s+and\s+Bankruptcy\s+Code|IBC)\b",
            re.I,
        ),
    ]

    # Indian Corporate Identification Number.
    CIN_PATTERN = re.compile(
        r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"
    )

    def __init__(self, config_path: str | Path = "config.yaml"):
        self.config = self._load_config(config_path)

        # ---------------------------------------------------------
        # Organization policy
        # ---------------------------------------------------------
        self.org_policy = self.config.get("organization_policy", {})

        self.redact_tier_1 = self.org_policy.get(
            "redact_tier_1", True
        )
        self.redact_tier_2 = self.org_policy.get(
            "redact_tier_2", False
        )
        self.redact_tier_3 = self.org_policy.get(
            "redact_tier_3", False
        )

        self.trust_patterns = [
            re.compile(pattern)
            for pattern in self.org_policy.get(
                "trust_patterns",
                [
                    r"\b[A-Za-z][A-Za-z\s]+Family\s+Trust\b",
                    r"\b[A-Za-z][A-Za-z\s]+Private\s+Trust\b",
                ],
            )
        ]

        self.tier_1_set = {
            value.lower().strip()
            for value in self.org_policy.get(
                "tier_1_entities", []
            )
        }

        self.tier_2_set = {
            value.lower().strip()
            for value in self.org_policy.get(
                "tier_2_entities", []
            )
        }

        self.tier_3_set = {
            value.lower().strip()
            for value in self.org_policy.get(
                "tier_3_entities", []
            )
        }

        # ---------------------------------------------------------
        # Regex patterns
        # ---------------------------------------------------------
        self._init_regexes()

        # ---------------------------------------------------------
        # spaCy
        # ---------------------------------------------------------
        nlp_config = self.config.get("nlp", {})

        model_name = nlp_config.get(
            "model",
            "en_core_web_sm",
        )

        self.batch_size = int(
            nlp_config.get("batch_size", 64)
        )

        try:
            self.nlp = spacy.load(model_name)
        except Exception:
            # Fallback for environments where the configured model
            # is unavailable.
            self.nlp = spacy.load("en_core_web_sm")

    @staticmethod
    def _load_config(
        config_path: str | Path,
    ) -> Dict[str, Any]:
        """Load YAML configuration if the file exists."""

        path = Path(config_path)

        if not path.exists():
            return {}

        with path.open("r", encoding="utf-8") as file:
            return yaml.safe_load(file) or {}

    # =================================================================
    # REGEX INITIALIZATION
    # =================================================================

    def _init_regexes(self) -> None:
        """Compile all structured-PII regex patterns."""

        # ---------------------------------------------------------
        # Email
        # ---------------------------------------------------------
        self.email_pattern = re.compile(
            r"\b[A-Za-z0-9._%+-]+"
            r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        )

        # ---------------------------------------------------------
        # Phone
        #
        # Supports examples such as:
        # +91 20 4505 3237
        # +91-9876543210
        # 020-45053237
        # 98765 43210
        # ---------------------------------------------------------
        self.phone_pattern = re.compile(
            r"""
            (?:
                # Labelled phone number
                \b(?:Tel|Telephone|Phone|Mob|Mobile|Fax|Contact)
                [\s.:\-_]+
                (?P<labelled>
                    (?:\+\s*91[\s-]?)?
                    (?:
                        \(?0?[1-9]\d{1,3}\)?[\s-]?
                    )?
                    \d[\d\s-]{6,14}\d
                )
            )
            |
            # Indian international mobile / landline
            (?<!\d)
            (?P<intl>
                \+\s*91[\s-]?
                (?:
                    [6-9]\d{4}[\s-]?\d{5}
                    |
                    \(?0?[1-9]\d{1,3}\)?[\s-]?
                    [1-9]\d{2,3}[\s-]?\d{3,4}
                )
            )
            |
            # Indian STD number
            (?<!\d)
            (?P<std>
                \(?0[1-9]\d{1,3}\)?[\s-]?
                [1-9]\d{2,3}[\s-]?\d{3,4}
            )
            |
            # Indian mobile number
            (?<!\d)
            (?P<mobile>
                [6-9]\d{4}[\s-]?\d{5}
            )
            """,
            re.IGNORECASE | re.VERBOSE,
        )

        # ---------------------------------------------------------
        # PAN
        # ---------------------------------------------------------
        self.pan_pattern = re.compile(
            r"\b[A-Z]{5}\d{4}[A-Z]\b"
        )

        # ---------------------------------------------------------
        # US SSN
        # ---------------------------------------------------------
        self.ssn_pattern = re.compile(
            r"\b"
            r"(?!000|666|9\d{2})\d{3}"
            r"-"
            r"(?!00)\d{2}"
            r"-"
            r"(?!0000)\d{4}"
            r"\b"
        )

        # ---------------------------------------------------------
        # Credit card candidates
        #
        # Luhn validation is performed after matching.
        # ---------------------------------------------------------
        self.credit_card_pattern = re.compile(
            r"""
            (?:
                \b(?:\d{4}[-\s]?){3}\d{1,4}\b
                |
                \b\d{13,19}\b
            )
            """,
            re.VERBOSE,
        )

        # ---------------------------------------------------------
        # IPv4
        # ---------------------------------------------------------
        self.ipv4_pattern = re.compile(
            r"\b"
            r"(?:(?:25[0-5]|2[0-4]\d|"
            r"[01]?\d?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)"
            r"\b"
        )

        # ---------------------------------------------------------
        # IPv6
        #
        # Covers standard full IPv6 notation. More compressed IPv6
        # forms can be added later if required.
        # ---------------------------------------------------------
        self.ipv6_pattern = re.compile(
            r"\b"
            r"(?:[0-9a-fA-F]{1,4}:){7}"
            r"[0-9a-fA-F]{1,4}"
            r"\b"
        )

        # ---------------------------------------------------------
        # Date of birth
        #
        # Only triggers when there is an explicit DOB/birth context.
        # This prevents normal financial dates from being classified
        # as DOB.
        # ---------------------------------------------------------
        self.dob_pattern = re.compile(
            r"""
            \b
            (?:date\s+of\s+birth|dob|born\s+on|birth\s+date)
            \b
            [\s:.\-–]+
            (
                # DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
                (?:0?[1-9]|[12]\d|3[01])
                [/\-.]
                (?:0?[1-9]|1[0-2])
                [/\-.]
                (?:19|20)\d{2}

                |

                # MM/DD/YYYY
                (?:0?[1-9]|1[0-2])
                [/\-.]
                (?:0?[1-9]|[12]\d|3[01])
                [/\-.]
                (?:19|20)\d{2}

                |

                # 14 August 1982
                (?:0?[1-9]|[12]\d|3[01])
                (?:st|nd|rd|th)?
                \s+
                (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)
                [a-z]*,?
                \s+
                (?:19|20)\d{2}

                |

                # August 14, 1982
                (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)
                [a-z]*
                \s+
                (?:0?[1-9]|[12]\d|3[01])
                (?:st|nd|rd|th)?
                ,?
                \s+
                (?:19|20)\d{2}
            )
            """,
            re.IGNORECASE | re.VERBOSE,
        )

        # ---------------------------------------------------------
        # URL
        # ---------------------------------------------------------
        self.url_pattern = re.compile(
            r"\b"
            r"(?:https?://|www\.)"
            r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
            r"(?:/[^\s<>\")']*)?",
            re.IGNORECASE,
        )

        # ---------------------------------------------------------
        # Address
        #
        # Intentionally conservative: requires an address cue and
        # an Indian 6-digit PIN code.
        # ---------------------------------------------------------
        # Address contextual cues
        # ---------------------------------------------------------
        self.address_cue_pattern = re.compile(
            r"""
            \b
            (?P<cue>
                (?:registered|corporate|head|correspondence|residential|branch)\s+office
                (?:\s+of\s+(?:our|the)\s+Company)?
                (?:\s+(?:is\s+)?(?:located|situated))?
                |
                (?:manufacturing\s+)?(?:facility|plant|works|unit)
                (?:\s+(?:is\s+)?(?:located|situated))?
                |
                (?:contact\s+details|details\s+of\s+(?:the\s+)?compliance\s+officer)
                (?:\s+are\s+as\s+set\s+forth\s+below)?
                |
                residing
                |
                resident\s+of
                |
                registrar
            )
            \s*(?::|at)\s*
            (?P<address>
                [^;\n\r]{8,180}?
                \b[1-9]\d{2}\s?\d{3}\b
            )
            """,
            re.IGNORECASE | re.VERBOSE,
        )

        self.standalone_address_pattern = re.compile(
            r"""
            \b
            (?P<address>
                (?:
                    (?:Gat|Plot|Survey|Unit|H\.?\s*No\.?|Door|Flat)\s*(?:No\.?)?\s*\d+[^;\n\r]{0,30}?
                    |
                    \d{1,4}[A-Za-z]?(?:/\d{1,4}[A-Za-z]?)*(?:\s*(?:and|,)\s*\d{1,4}[A-Za-z]?)*\s*,?\s*
                )
                (?:Village|Gat|Plot|Survey|Taluka|Road|Marg|Street|Floor|Bldg|Building|Industrial\s+Area|MIDC)
                [^;\n\r]{5,120}?
                \b[1-9]\d{2}\s?\d{3}\b
            )
            """,
            re.IGNORECASE | re.VERBOSE,
        )

        # ---------------------------------------------------------
        # Person contextual cues
        # ---------------------------------------------------------
        self.person_cue_pattern = re.compile(
            r"""
            (?:
                \b
                (?i:contact\s+person|shareholder|promoter|director|
                compliance\s+officer|chief\s+financial\s+officer)
                [\s:\-]+
                (
                    [A-Z][a-z]+
                    (?:\s+[A-Z][a-z]+){1,3}
                )
            )
            |
            (?:
                \b
                (?i:Mr\.|Mrs\.|Ms\.|Dr\.|Shri|Smt\.)
                \s+
                (
                    [A-Z][a-z]+
                    (?:\s+[A-Z][a-z]+){1,3}
                )
            )
            """,
            re.VERBOSE,
        )

        # ---------------------------------------------------------
        # Known people from the assignment document
        # ---------------------------------------------------------
        self.promoter_names = [
            "Kushal Subbayya Hegde",
            "Kushal Hegde",
            "Pushpa Kushal Hegde",
            "Pushpa Hegde",
            "Rajesh Kushal Hegde",
            "Rajesh Hegde",
            "Rohit Kushal Hegde",
            "Rohit Hegde",
            "Rakhi Girija Shetty",
            "Rakhi Shetty",
            "Sarthak Malvadkar",
            "Sandesh Bhagwat",
            "Amod Joshi",
            "Prakash Boricha",
            "Lokesh Shah",
            "Soumavo Sarkar",
            "Dinesh Hirachand Munot",
        ]

        sorted_promoters = sorted(
            self.promoter_names,
            key=len,
            reverse=True,
        )

        promoter_regex = (
            r"\b(?:"
            + "|".join(
                re.escape(name)
                for name in sorted_promoters
            )
            + r")\b"
        )

        self.promoter_pattern = re.compile(
            promoter_regex,
            re.IGNORECASE,
        )

        # ---------------------------------------------------------
        # Explicit Tier 1 organizations
        # ---------------------------------------------------------
        tier_1_names = [
            name
            for name in self.org_policy.get(
                "tier_1_entities", []
            )
            if len(name.strip()) > 3
        ]

        if tier_1_names:
            self.tier_1_pattern = re.compile(
                r"\b(?:"
                + "|".join(
                    re.escape(name)
                    for name in tier_1_names
                )
                + r")\b",
                re.IGNORECASE,
            )
        else:
            self.tier_1_pattern = None

        # ---------------------------------------------------------
        # Common false PERSON terms
        # ---------------------------------------------------------
        self.false_person_terms = {
            "equity shares",
            "promoter group",
            "board of directors",
            "key managerial personnel",
            "registered office",
            "corporate office",
            "statutory auditor",
            "audit committee",
            "financial statements",
            "objects of the offer",
            "restated consolidated",
            "drhp",
            "rhp",
            "rupees",
            "crores",
            "lakhs",
            "limited",
            "private limited",
            "public issue",
            "offer",
            "company secretary",
            "tel",
            "telephone",
            "mob",
            "mobile",
            "fax",
            "phone",
            "email",
            "e-mail",
            "e - mail",
            "mail",
            "website",
            "web",
            "shareholders",
            "shareholder",
            "promoters",
            "promoter",
            "directors",
            "director",
            "promoter selling shareholders",
            "selling shareholders",
            "selling shareholder",
            "independent directors",
            "independent director",
            "non-executive directors",
            "non-executive director",
            "executive directors",
            "executive director",
            "our company",
            "the company",
            "group",
            "clause",
            "section",
            "pursuant",
            "secondary transfer",
            "secondary transfer of",
            "excludes",
            "supa facility",
        }

    # =================================================================
    # ORGANIZATION CLASSIFICATION
    # =================================================================

    def classify_organization(self, org_name: str) -> int:
        """
        Classify an organization.

        Tier 1:
            Assignment-specific entities that should be redacted.

        Tier 2:
            Commercial entities that are not automatically redacted.

        Tier 3:
            Regulatory/statutory organizations that should be preserved.
        """

        clean = org_name.strip()
        norm = clean.lower()

        # Tier 3 always wins.
        if norm in self.tier_3_set:
            return 3

        for entity in self.tier_3_set:
            if entity in norm or norm in entity:
                return 3

        # Tier 1.
        if norm in self.tier_1_set:
            return 1

        for entity in self.tier_1_set:
            if entity in norm:
                return 1

        # Named family trusts.
        for pattern in self.trust_patterns:
            if pattern.search(clean):
                return 1

        # Tier 2.
        if norm in self.tier_2_set:
            return 2

        for entity in self.tier_2_set:
            if entity in norm:
                return 2

        # Unknown organizations default to Tier 2.
        # This prevents aggressive redaction of every company
        # mentioned in a financial prospectus.
        return 2

    # =================================================================
    # SUPPRESSION RULES
    # =================================================================

    def should_suppress(
        self,
        entity: PiiEntity,
        full_text: str,
    ) -> bool:
        """Return True when a detected entity is likely a false positive."""

        text = entity.text.strip()
        category = entity.category

        # ---------------------------------------------------------
        # Statutory acts
        # ---------------------------------------------------------
        if category in {"ORGANIZATION", "PERSON"}:
            for pattern in self.STATUTORY_ACT_PATTERNS:
                if pattern.search(text):
                    return True

        # ---------------------------------------------------------
        # Document identifiers and URLs (Preserved / Non-PII)
        #
        # Prevent PANs, URLs, etc. from being misidentified by
        # spaCy as PERSON or ORGANIZATION.
        # ---------------------------------------------------------
        if category in {"ORGANIZATION", "PERSON"}:
            if self.pan_pattern.search(text) or self.url_pattern.search(text):
                return True

        # ---------------------------------------------------------
        # DIN
        #
        # DIN is an 8-digit director identifier, not a phone number.
        # ---------------------------------------------------------
        if category == "PHONE":
            digits = re.sub(r"\D", "", text)

            if len(digits) == 8:
                start = max(0, entity.start_char - 30)
                end = min(
                    len(full_text),
                    entity.end_char + 30,
                )

                surrounding = full_text[start:end].upper()

                if (
                    "DIN" in surrounding
                    or "DIRECTOR IDENTIFICATION NUMBER"
                    in surrounding
                ):
                    return True

        # ---------------------------------------------------------
        # Numeric identifiers
        #
        # Avoid treating order numbers, invoice numbers, IDs,
        # policy numbers, folio numbers etc. as PII.
        # ---------------------------------------------------------
        if category in {"PHONE", "CREDIT_CARD"}:
            start = max(0, entity.start_char - 50)

            prefix = full_text[
                start:entity.start_char
            ].lower()

            identifier_context = re.search(
                r"""
                \b
                (?:
                    order
                    |invoice
                    |reference
                    |ref
                    |account
                    |acc
                    |purchase\s+order
                    |po
                    |voucher
                    |policy
                    |cheque
                    |check
                    |serial
                    |id
                    |tracking
                    |folio
                    |ticket
                )
                \s*
                (?:no\.?|number|num)?
                \s*
                [#:.#\-\s]*
                $
                """,
                prefix,
                re.IGNORECASE | re.VERBOSE,
            )

            if identifier_context:
                return True

        # ---------------------------------------------------------
        # CIN
        # ---------------------------------------------------------
        if self.CIN_PATTERN.search(text):
            return True

        # ---------------------------------------------------------
        # Organization policy
        # ---------------------------------------------------------
        if category == "ORGANIZATION":
            tier = self.classify_organization(text)

            if tier == 1 and not self.redact_tier_1:
                return True

            if tier == 2 and not self.redact_tier_2:
                return True

            if tier == 3 and not self.redact_tier_3:
                return True

        # ---------------------------------------------------------
        # PERSON false positives
        # ---------------------------------------------------------
        if category == "PERSON":
            normalized = re.sub(
                r"\s+",
                " ",
                text.lower(),
            ).strip()

            if (
                normalized in self.false_person_terms
                or len(normalized) <= 2
                or normalized.isdigit()
            ):
                return True

            false_location_terms = (
                "taluka",
                "district",
                "village",
                "marg",
                "road",
                "street",
                "offer",
                "clause",
                "page",
            )

            if any(
                term in normalized
                for term in false_location_terms
            ):
                return True

            if any(char.isdigit() for char in text):
                return True

        # ---------------------------------------------------------
        # Standalone geographic locations
        # ---------------------------------------------------------
        if category == "ADDRESS":
            normalized = text.lower().strip()

            standalone_locations = {
                "india",
                "republic of india",
                "maharashtra",
                "gujarat",
                "karnataka",
                "tamil nadu",
                "delhi",
                "mumbai",
                "pune",
                "bengaluru",
                "chennai",
                "kolkata",
            }

            if normalized in standalone_locations:
                return True

        return False

    # =================================================================
    # REGEX DETECTION
    # =================================================================

    def detect_regex(
        self,
        text: str,
    ) -> List[PiiEntity]:
        """Detect structured PII using regex patterns."""

        entities: List[PiiEntity] = []

        # ---------------------------------------------------------
        # EMAIL
        # ---------------------------------------------------------
        for match in self.email_pattern.finditer(text):
            entities.append(
                PiiEntity(
                    text=match.group(),
                    category="EMAIL",
                    start_char=match.start(),
                    end_char=match.end(),
                    confidence=1.0,
                    detector="regex_email",
                )
            )

        # ---------------------------------------------------------
        # PHONE
        # ---------------------------------------------------------
        for match in self.phone_pattern.finditer(text):
            raw = None
            start = None
            end = None

            for group_name in (
                "labelled",
                "intl",
                "std",
                "mobile",
            ):
                if match.group(group_name):
                    raw = match.group(group_name)
                    start = match.start(group_name)
                    end = match.end(group_name)
                    break

            if not raw or start is None or end is None:
                continue

            cleaned = raw.strip(
                ".,;:()[]{} "
            )

            if not cleaned:
                continue

            digit_count = len(
                re.sub(r"\D", "", cleaned)
            )

            # Phone numbers should have enough digits to be
            # meaningful, but we intentionally do not require
            # exactly 10 digits because landlines vary.
            if digit_count < 8:
                continue

            leading_trim = len(raw) - len(
                raw.lstrip(".,;:()[]{} ")
            )

            trailing_trim = len(raw) - len(
                raw.rstrip(".,;:()[]{} ")
            )

            final_start = start + leading_trim
            final_end = end - trailing_trim

            entities.append(
                PiiEntity(
                    text=cleaned,
                    category="PHONE",
                    start_char=final_start,
                    end_char=final_end,
                    confidence=0.90,
                    detector="regex_phone",
                )
            )

        # ---------------------------------------------------------
        # PAN (Preserved / Non-PII document identifier)
        #
        # Recognized via self.pan_pattern for non-PII suppression;
        # not emitted as a PII redaction entity.
        # ---------------------------------------------------------

        # ---------------------------------------------------------
        # SSN
        # ---------------------------------------------------------
        for match in self.ssn_pattern.finditer(text):
            entities.append(
                PiiEntity(
                    text=match.group(),
                    category="SSN",
                    start_char=match.start(),
                    end_char=match.end(),
                    confidence=0.95,
                    detector="regex_ssn",
                )
            )

        # ---------------------------------------------------------
        # CREDIT CARD
        # ---------------------------------------------------------
        for match in self.credit_card_pattern.finditer(text):
            candidate = match.group()
            digits = re.sub(r"\D", "", candidate)

            if not luhn_checksum(digits):
                continue

            # Reject numbers that are embedded in larger digit
            # sequences.
            before = (
                text[match.start() - 1]
                if match.start() > 0
                else ""
            )
            after = (
                text[match.end()]
                if match.end() < len(text)
                else ""
            )

            if before.isdigit() or after.isdigit():
                continue

            entities.append(
                PiiEntity(
                    text=candidate,
                    category="CREDIT_CARD",
                    start_char=match.start(),
                    end_char=match.end(),
                    confidence=0.95,
                    detector="regex_credit_card",
                )
            )

        # ---------------------------------------------------------
        # IPv4
        # ---------------------------------------------------------
        for match in self.ipv4_pattern.finditer(text):
            ip = match.group()

            octets = ip.split(".")

            if len(octets) != 4:
                continue

            if not all(
                0 <= int(octet) <= 255
                for octet in octets
            ):
                continue

            # 0.0.0.0 is generally a special technical address,
            # not useful as personal information.
            if ip == "0.0.0.0":
                continue

            entities.append(
                PiiEntity(
                    text=ip,
                    category="IP_ADDRESS",
                    start_char=match.start(),
                    end_char=match.end(),
                    confidence=0.90,
                    detector="regex_ipv4",
                )
            )

        # ---------------------------------------------------------
        # IPv6
        # ---------------------------------------------------------
        for match in self.ipv6_pattern.finditer(text):
            entities.append(
                PiiEntity(
                    text=match.group(),
                    category="IP_ADDRESS",
                    start_char=match.start(),
                    end_char=match.end(),
                    confidence=0.95,
                    detector="regex_ipv6",
                )
            )

        # ---------------------------------------------------------
        # DOB
        # ---------------------------------------------------------
        for match in self.dob_pattern.finditer(text):
            dob = match.group(1).strip()

            entities.append(
                PiiEntity(
                    text=dob,
                    category="DOB",
                    start_char=match.start(1),
                    end_char=match.end(1),
                    confidence=0.90,
                    detector="regex_dob",
                )
            )

        # ---------------------------------------------------------
        # URL (Preserved / Non-PII document identifier)
        #
        # Recognized via self.url_pattern for non-PII suppression;
        # not emitted as a PII redaction entity.
        # ---------------------------------------------------------

        # ---------------------------------------------------------
        # ADDRESS
        # ---------------------------------------------------------
        for match in self.address_cue_pattern.finditer(text):
            address = match.group("address").strip(
                ".,;: "
            )

            if not address:
                continue

            start = match.start("address")

            # Normalize contextual prefixes like "at " so the canonical
            # physical address is identified consistently.
            if address.lower().startswith("at "):
                address = address[3:].strip(".,;: ")
                prefix_offset = match.group("address").lower().find(address.lower())
                if prefix_offset != -1:
                    start = match.start("address") + prefix_offset

            end = start + len(address)

            entities.append(
                PiiEntity(
                    text=address,
                    category="ADDRESS",
                    start_char=start,
                    end_char=end,
                    confidence=0.90,
                    detector="regex_address",
                )
            )

        for match in self.standalone_address_pattern.finditer(text):
            address = match.group("address").strip(
                ".,;: "
            )

            if not address:
                continue

            start = match.start("address")
            end = start + len(address)

            entities.append(
                PiiEntity(
                    text=address,
                    category="ADDRESS",
                    start_char=start,
                    end_char=end,
                    confidence=0.85,
                    detector="regex_standalone_address",
                )
            )

        # ---------------------------------------------------------
        # PERSON CONTEXT
        # ---------------------------------------------------------
        date_words = {
            "dated", "date", "annual", "quarterly", "monthly", "fiscal",
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        }

        for match in self.person_cue_pattern.finditer(text):
            name = (
                match.group(1)
                or match.group(2)
                or ""
            ).strip()

            if not name:
                continue

            if name.lower() in self.false_person_terms:
                continue

            # Person names must not contain date tokens or calendar months
            # (e.g. "director dated August 16" or "promoter January 2025")
            if any(w in date_words for w in name.lower().split()):
                continue

            # Use the actual captured group's position rather than
            # text.find(), which can return an earlier occurrence.
            if match.group(1):
                start = match.start(1)
                end = match.end(1)
            else:
                start = match.start(2)
                end = match.end(2)

            entities.append(
                PiiEntity(
                    text=name,
                    category="PERSON",
                    start_char=start,
                    end_char=end,
                    confidence=0.90,
                    detector="regex_person",
                )
            )

        # ---------------------------------------------------------
        # Known people
        # ---------------------------------------------------------
        for match in self.promoter_pattern.finditer(text):
            entities.append(
                PiiEntity(
                    text=match.group(),
                    category="PERSON",
                    start_char=match.start(),
                    end_char=match.end(),
                    confidence=1.0,
                    detector="known_person",
                )
            )

        # ---------------------------------------------------------
        # Explicit Tier 1 organizations
        # ---------------------------------------------------------
        if self.tier_1_pattern:
            for match in self.tier_1_pattern.finditer(text):
                entities.append(
                    PiiEntity(
                        text=match.group(),
                        category="ORGANIZATION",
                        start_char=match.start(),
                        end_char=match.end(),
                        confidence=1.0,
                        detector="tier_1_explicit",
                        tier=1,
                    )
                )

        # ---------------------------------------------------------
        # Family trusts
        # ---------------------------------------------------------
        for pattern in self.trust_patterns:
            for match in pattern.finditer(text):
                entities.append(
                    PiiEntity(
                        text=match.group().strip(),
                        category="ORGANIZATION",
                        start_char=match.start(),
                        end_char=match.end(),
                        confidence=1.0,
                        detector="trust_regex",
                        tier=1,
                    )
                )

        return entities

    # =================================================================
    # SPACY
    # =================================================================

    def _convert_spacy_doc_entities(
        self,
        doc,
    ) -> List[PiiEntity]:
        """Convert spaCy NER results into PiiEntity objects."""

        entities: List[PiiEntity] = []

        for ent in doc.ents:
            if ent.label_ not in {
                "PERSON",
                "ORG",
                "GPE",
                "LOC",
                "FAC",
            }:
                continue

            clean = ent.text.strip(
                ".,;:()[]{}\"' "
            )

            if len(clean) <= 1:
                continue

            leading = ent.text.find(clean)

            start = ent.start_char

            if leading > 0:
                start += leading

            end = start + len(clean)

            normalized = clean.lower()

            # Strong organization indicators.
            organization_terms = (
                "limited",
                "ltd",
                "private limited",
                "pvt",
                "llp",
                "trust",
                "corporation",
                "company",
            )

            if any(
                term in normalized
                for term in organization_terms
            ):
                category = "ORGANIZATION"
            elif ent.label_ == "PERSON":
                category = "PERSON"
            elif ent.label_ == "ORG":
                category = "ORGANIZATION"
            else:
                # GPE/LOC/FAC is not automatically an address.
                # Only classify it as ADDRESS when contextual rules
                # later identify it as part of an address.
                continue

            confidence = (
                0.85
                if category in {
                    "PERSON",
                    "ORGANIZATION",
                }
                else 0.80
            )

            entities.append(
                PiiEntity(
                    text=clean,
                    category=category,
                    start_char=start,
                    end_char=end,
                    confidence=confidence,
                    detector="spacy_ner",
                )
            )

        return entities

    def detect_spacy(
        self,
        text: str,
    ) -> List[PiiEntity]:
        """Detect PERSON and ORGANIZATION entities with spaCy."""

        if not text.strip():
            return []

        doc = self.nlp(text)

        return self._convert_spacy_doc_entities(doc)

    # =================================================================
    # FILTERING + OVERLAP RESOLUTION
    # =================================================================

    @staticmethod
    def _priority(category: str) -> int:
        """Return replacement priority for overlapping entities."""

        return {
            "EMAIL": 100,
            "PHONE": 95,
            "PAN": 95,
            "SSN": 95,
            "CREDIT_CARD": 95,
            "IP_ADDRESS": 90,
            "DOB": 85,
            "URL": 80,
            "PERSON": 75,
            "ORGANIZATION": 70,
            "ADDRESS": 60,
        }.get(category, 50)

    def filter_and_resolve(
        self,
        entities: List[PiiEntity],
        full_text: str,
    ) -> List[PiiEntity]:
        """Suppress false positives and resolve overlapping spans."""

        filtered: List[PiiEntity] = []

        for entity in entities:
            if entity.start_char < 0:
                continue

            if entity.end_char > len(full_text):
                continue

            if entity.start_char >= entity.end_char:
                continue

            actual_text = full_text[
                entity.start_char:entity.end_char
            ]

            if actual_text != entity.text:
                if actual_text.strip(
                    ".,;:()[]{} "
                ) != entity.text.strip(
                    ".,;:()[]{} "
                ):
                    continue

            if self.should_suppress(
                entity,
                full_text,
            ):
                continue

            if entity.category == "ORGANIZATION":
                tier = self.classify_organization(
                    entity.text
                )
                entity = PiiEntity(
                    text=entity.text,
                    category=entity.category,
                    start_char=entity.start_char,
                    end_char=entity.end_char,
                    confidence=entity.confidence,
                    detector=entity.detector,
                    tier=tier,
                )

            filtered.append(entity)

        # Ensure that no PERSON or ORGANIZATION overlaps with any EMAIL span
        email_spans = [
            (e.start_char, e.end_char)
            for e in filtered
            if e.category == "EMAIL"
        ]
        if email_spans:
            filtered = [
                e for e in filtered
                if not (
                    e.category in {"PERSON", "ORGANIZATION"}
                    and any(
                        max(e.start_char, em_start) < min(e.end_char, em_end)
                        for em_start, em_end in email_spans
                    )
                )
            ]

        # Sort by:
        # 1. start position
        # 2. priority
        # 3. longer span
        sorted_entities = sorted(
            filtered,
            key=lambda entity: (
                entity.start_char,
                -self._priority(entity.category),
                -(entity.end_char - entity.start_char),
            ),
        )

        resolved: List[PiiEntity] = []

        for entity in sorted_entities:
            if not resolved:
                resolved.append(entity)
                continue

            last = resolved[-1]

            if entity.start_char < last.end_char:
                # If an ADDRESS contains a PERSON/ORGANIZATION,
                # keep the complete address. The nested entity is
                # already covered by address redaction.
                if (
                    last.category == "ADDRESS"
                    and entity.category in {"PERSON", "ORGANIZATION"}
                    and entity.end_char <= last.end_char
                ):
                    continue

                if (
                    entity.category == "ADDRESS"
                    and last.category in {"PERSON", "ORGANIZATION"}
                    and last.end_char <= entity.end_char
                ):
                    resolved[-1] = entity
                    continue

                # Existing priority-based overlap handling
                if self._priority(entity.category) > self._priority(
                    last.category
                ):
                    resolved[-1] = entity

                continue

            resolved.append(entity)

        return resolved

    # =================================================================
    # PUBLIC API
    # =================================================================

    def detect(
        self,
        text: str,
    ) -> List[PiiEntity]:
        """Detect all PII from a single text block."""

        if not text or not text.strip():
            return []

        regex_entities = self.detect_regex(text)
        spacy_entities = self.detect_spacy(text)

        return self.filter_and_resolve(
            regex_entities + spacy_entities,
            text,
        )

    def detect_batch(
        self,
        texts: List[str],
    ) -> List[List[PiiEntity]]:
        """Detect PII across multiple text blocks using spaCy batching."""

        if not texts:
            return []

        results: List[List[PiiEntity]] = [
            []
            for _ in texts
        ]

        # spaCy does not need to process empty or purely numeric
        # cells because structured PII is handled by regex.
        alpha_indices = [
            index
            for index, text in enumerate(texts)
            if text and any(
                character.isalpha()
                for character in text
            )
        ]

        alpha_texts = [
            texts[index]
            for index in alpha_indices
        ]

        spacy_results: List[List[PiiEntity]] = [
            []
            for _ in texts
        ]

        if alpha_texts:
            for index, doc in zip(
                alpha_indices,
                self.nlp.pipe(
                    alpha_texts,
                    batch_size=self.batch_size,
                ),
            ):
                spacy_results[index] = (
                    self._convert_spacy_doc_entities(doc)
                )

        # Regex + spaCy + suppression.
        for index, text in enumerate(texts):
            if not text or not text.strip():
                results[index] = []
                continue

            regex_entities = self.detect_regex(text)

            raw_entities = (
                regex_entities
                + spacy_results[index]
            )

            results[index] = self.filter_and_resolve(
                raw_entities,
                text,
            )

        return results