"""PII redaction engine for DOCX documents.

Uses consistent synthetic replacements and character-offset splicing
to redact PII while preserving the document's existing structure,
tables, and run formatting where possible.
"""

from dataclasses import dataclass
import logging
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docx.text.paragraph import Paragraph
from faker import Faker
import yaml

from detector import PiiDetector, PiiEntity
from extractor import DocxExtractor


logger = logging.getLogger(__name__)


@dataclass
class RedactionAudit:
    """Record one successful redaction."""

    original: str
    replacement: str
    category: str
    location: str


class EntityResolver:
    """Maps repeated entity variations to a stable canonical ID."""

    HONORIFICS = [
        re.compile(
            r"^(?:mr|mrs|ms|dr|late|shri|smt|ca|adv|prof)\.?\s+",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:director|promoter|chairman|managing\s+director)\s+",
            re.IGNORECASE,
        ),
    ]

    CORP_SUFFIXES = [
        re.compile(
            r"\b(?:private\s+limited|pvt\.?\s*ltd\.?|limited|ltd\.?|llp|inc\.?)\b",
            re.IGNORECASE,
        ),
    ]

    def __init__(self) -> None:
        self.canonical_names: Dict[str, str] = {}
        self.alias_to_id: Dict[Tuple[str, str], str] = {}

    def normalize(self, text: str, category: str) -> str:
        """Normalize text for entity matching."""

        clean = text.strip()

        if category == "PERSON":
            for pattern in self.HONORIFICS:
                clean = pattern.sub("", clean)

        elif category == "ORGANIZATION":
            for pattern in self.CORP_SUFFIXES:
                clean = pattern.sub("", clean)

        clean = " ".join(clean.split())

        return clean.strip(".,;:()\"' ")

    def resolve(self, text: str, category: str) -> str:
        """Return a stable canonical ID for an entity."""

        surface_key = (
            category,
            text.strip(),
        )

        if surface_key in self.alias_to_id:
            return self.alias_to_id[surface_key]

        normalized = self.normalize(
            text,
            category,
        )

        normalized_key = (
            category,
            normalized.lower(),
        )

        if normalized_key in self.alias_to_id:
            entity_id = self.alias_to_id[normalized_key]

            self.alias_to_id[surface_key] = entity_id

            return entity_id

        # Conservative token matching for person names.
        #
        # Example:
        # "Kushal Hegde" -> "Kushal Subbayya Hegde"
        if category == "PERSON":

            tokens = set(
                normalized.lower().split()
            )

            if len(tokens) >= 2:

                for (
                    entity_id,
                    canonical_name,
                ) in self.canonical_names.items():

                    canonical_tokens = set(
                        canonical_name.lower().split()
                    )

                    if (
                        tokens.issubset(canonical_tokens)
                        or canonical_tokens.issubset(tokens)
                    ):
                        self.alias_to_id[
                            surface_key
                        ] = entity_id

                        self.alias_to_id[
                            normalized_key
                        ] = entity_id

                        return entity_id

        entity_id = (
            f"{category}_"
            f"{len(self.canonical_names) + 1:04d}"
        )

        self.canonical_names[
            entity_id
        ] = normalized

        self.alias_to_id[
            surface_key
        ] = entity_id

        self.alias_to_id[
            normalized_key
        ] = entity_id

        return entity_id


class FakeDataGenerator:
    """Generate deterministic synthetic replacements."""

    TRUST_PREFIXES = [
        "Nilgiri",
        "Vindhya",
        "Sahyadri",
        "Aravalli",
        "Satpura",
        "Kailash",
        "Trishul",
        "Nanda Devi",
        "Kamet",
        "Sivalik",
    ]

    CORP_NAMES = [
        "Sterling Horizon Industries Private Limited",
        "Vanguard Dynamics Logistics Private Limited",
        "Pinnacle Synergy Enterprises Private Limited",
        "Zenith Crescent Capital Private Limited",
        "Matrix Frontier Infra Private Limited",
        "Nexus Orbit Global Private Limited",
        "Titan Blue Industrial Holdings Private Limited",
        "Beacon Crest Manufacturing Private Limited",
    ]

    def __init__(
        self,
        seed: int = 42,
        locale: str = "en_IN",
    ) -> None:

        self.faker = Faker(locale)

        Faker.seed(seed)
        random.seed(seed)

        self.trust_index = 0
        self.corp_index = 0

        self.fake_domain = "apexzenith.com"

        self.fake_company = (
            "Apex Zenith Technologies Limited"
        )

        self.fake_company_short = (
            "Apex Zenith"
        )

    def person_name(self) -> str:
        """Generate a fake Indian-style name."""

        return (
            f"{self.faker.first_name()} "
            f"{self.faker.last_name()}"
        )

    def company_name(
        self,
        is_trust: bool = False,
    ) -> str:
        """Generate a fake organization name."""

        if is_trust:

            prefix = self.TRUST_PREFIXES[
                self.trust_index
                % len(self.TRUST_PREFIXES)
            ]

            self.trust_index += 1

            return (
                f"{prefix} Family Trust"
            )

        name = self.CORP_NAMES[
            self.corp_index
            % len(self.CORP_NAMES)
        ]

        self.corp_index += 1

        return name

    def email(
        self,
        person_hint: Optional[str] = None,
    ) -> str:
        """Generate a fake email address ensuring original username is never leaked."""

        user = f"{self.faker.first_name().lower()}.{self.faker.last_name().lower()}"
        return f"{user}@{self.fake_domain}"

    def phone(
        self,
        hint: str = "",
    ) -> str:
        """Generate a fake Indian phone number."""

        if (
            "020" in hint
            or "+91 20" in hint
        ):

            number = random.randint(
                40_000_000,
                49_999_999,
            )

            digits = str(number)

            return (
                f"+91 20 "
                f"{digits[:4]} "
                f"{digits[4:]}"
            )

        if (
            "022" in hint
            or "+91 22" in hint
        ):

            number = random.randint(
                40_000_000,
                49_999_999,
            )

            digits = str(number)

            return (
                f"+91 22 "
                f"{digits[:4]} "
                f"{digits[4:]}"
            )

        return (
            f"+91 "
            f"{random.choice(['98', '97', '99'])}"
            f"{random.randint(10_000_000, 99_999_999)}"
        )

    def pan(self) -> str:
        """Generate a fake PAN."""

        letters = "".join(
            random.choices(
                "ABCDEFGHJKLMNPQRSTUVWXYZ",
                k=5,
            )
        )

        numbers = "".join(
            random.choices(
                "0123456789",
                k=4,
            )
        )

        last = random.choice(
            "ABCDEFGHJKLMNPQRSTUVWXYZ"
        )

        return (
            f"{letters}"
            f"{numbers}"
            f"{last}"
        )

    def address(self) -> str:
        """Generate a fake Indian address."""

        street = self.faker.street_address()

        city = random.choice(
            [
                "Pune",
                "Chakan",
                "Mumbai",
            ]
        )

        if city in (
            "Pune",
            "Chakan",
        ):
            pin = random.randint(
                411001,
                411058,
            )
        else:
            pin = random.randint(
                400001,
                400099,
            )

        return (
            f"Plot No. "
            f"{random.randint(10, 150)}, "
            f"{street}, "
            f"{city} - {pin}"
        )

    def ssn(self) -> str:
        """Generate a fake SSN."""

        return self.faker.ssn()

    def credit_card(self) -> str:
        """Generate a fake credit-card number."""

        return self.faker.credit_card_number()

    def ipv4(self) -> str:
        """Generate a private IPv4 address."""

        return (
            f"10.{random.randint(0, 255)}."
            f"{random.randint(1, 254)}."
            f"{random.randint(1, 254)}"
        )

    def url(self) -> str:
        """Generate a fake URL."""

        return (
            f"https://www.{self.fake_domain}"
        )


class PiiRedactor:
    """Apply consistent synthetic replacements to detected PII."""

    def __init__(
        self,
        config_path: str | Path = "config.yaml",
    ) -> None:

        config = self._load_config(
            config_path
        )

        replacement_config = config.get(
            "replacement",
            {},
        )

        seed = replacement_config.get(
            "seed",
            42,
        )

        locale = replacement_config.get(
            "locale",
            "en_IN",
        )

        self.resolver = EntityResolver()

        self.generator = FakeDataGenerator(
            seed=seed,
            locale=locale,
        )

        # (category, canonical_id) -> replacement
        self.replacement_cache: Dict[
            Tuple[str, str],
            str,
        ] = {}

        # (category, original surface text) -> replacement
        self.surface_cache: Dict[
            Tuple[str, str],
            str,
        ] = {}

    @staticmethod
    def _load_config(
        config_path: str | Path,
    ) -> Dict[str, Any]:
        """Load YAML configuration."""

        path = Path(config_path)

        if not path.exists():
            return {}

        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            return yaml.safe_load(file) or {}

    def get_replacement(
        self,
        entity: PiiEntity,
    ) -> str:
        """Return a stable replacement for the entity."""

        category = entity.category

        original = entity.text.strip()

        surface_key = (
            category,
            original,
        )

        if surface_key in self.surface_cache:
            return self.surface_cache[
                surface_key
            ]

        canonical_id = self.resolver.resolve(
            original,
            category,
        )

        canonical_key = (
            category,
            canonical_id,
        )

        if canonical_key in self.replacement_cache:

            replacement = (
                self.replacement_cache[
                    canonical_key
                ]
            )

            self.surface_cache[
                surface_key
            ] = replacement

            return replacement

        if category == "PERSON":

            replacement = (
                self.generator.person_name()
            )

        elif category == "ORGANIZATION":

            if original == "KSH":

                replacement = (
                    self.generator.fake_company_short
                )

            elif "ksh" in original.lower():

                replacement = (
                    self.generator.fake_company
                )

            else:

                replacement = (
                    self.generator.company_name(
                        is_trust=(
                            "trust"
                            in original.lower()
                        )
                    )
                )

        elif category == "EMAIL":

            local_part = original.split(
                "@",
                1,
            )[0]

            replacement = (
                self.generator.email(
                    person_hint=local_part
                )
            )

        elif category == "PHONE":

            replacement = (
                self.generator.phone(
                    hint=original
                )
            )

        elif category == "PAN":

            replacement = (
                self.generator.pan()
            )

        elif category == "ADDRESS":

            replacement = (
                self.generator.address()
            )

        elif category == "SSN":

            replacement = (
                self.generator.ssn()
            )

        elif category == "CREDIT_CARD":

            replacement = (
                self.generator.credit_card()
            )

        elif category == "IP_ADDRESS":

            replacement = (
                self.generator.ipv4()
            )

        elif category == "URL":

            replacement = (
                self.generator.url()
            )

        elif category == "DOB":

            replacement = "May 14, 1978"

        else:

            replacement = (
                f"[REDACTED_{category}]"
            )

        self.replacement_cache[
            canonical_key
        ] = replacement

        self.surface_cache[
            surface_key
        ] = replacement

        return replacement

    def splice_runs(
        self,
        paragraph: Paragraph,
        start_char: int,
        end_char: int,
        replacement: str,
    ) -> bool:
        """Replace a character span while preserving run formatting."""

        paragraph_text = paragraph.text or ""

        logger.debug(
            "[SPLICE START] "
            "span=(%d,%d) "
            "replacement=%r "
            "paragraph_len=%d "
            "paragraph_text=%r",
            start_char,
            end_char,
            replacement,
            len(paragraph_text),
            paragraph_text[:300],
        )

        if start_char < 0 or end_char <= start_char:

            logger.debug(
                "[SPLICE FAIL] Invalid span: "
                "start=%d end=%d",
                start_char,
                end_char,
            )

            return False

        # -------------------------------------------------
        # Build character spans for every run.
        # -------------------------------------------------

        run_spans = []

        offset = 0

        for index, run in enumerate(
            paragraph.runs
        ):

            text = run.text or ""

            length = len(text)

            run_spans.append(
                (
                    index,
                    offset,
                    offset + length,
                    run,
                )
            )

            logger.debug(
                "[RUN] index=%d "
                "span=(%d,%d) "
                "len=%d "
                "text=%r",
                index,
                offset,
                offset + length,
                length,
                text[:150],
            )

            offset += length

        # -------------------------------------------------
        # Compare paragraph.text against concatenated runs.
        # -------------------------------------------------

        runs_text = "".join(
            run.text or ""
            for run in paragraph.runs
        )

        logger.debug(
            "[TEXT CHECK] "
            "paragraph_text_len=%d "
            "runs_text_len=%d "
            "equal=%s",
            len(paragraph_text),
            len(runs_text),
            paragraph_text == runs_text,
        )

        if paragraph_text != runs_text:

            logger.debug(
                "[TEXT MISMATCH] "
                "paragraph.text=%r "
                "runs_text=%r",
                paragraph_text[:500],
                runs_text[:500],
            )

        # -------------------------------------------------
        # Entity must fit inside the run text.
        # -------------------------------------------------

        if end_char > offset:

            logger.debug(
                "[SPLICE FAIL] "
                "Entity end=%d exceeds "
                "run_text_length=%d",
                end_char,
                offset,
            )

            return False

        # -------------------------------------------------
        # Check what text exists at the detected offsets.
        # -------------------------------------------------

        detected_text = paragraph_text[
            start_char:end_char
        ]

        logger.debug(
            "[ENTITY TEXT CHECK] "
            "span=(%d,%d) "
            "detected_span=%r",
            start_char,
            end_char,
            detected_text,
        )

        # -------------------------------------------------
        # Resolve start/end runs.
        # -------------------------------------------------

        start_run_index = -1
        end_run_index = -1

        for (
            index,
            run_start,
            run_end,
            run,
        ) in run_spans:

            if (
                run_start
                <= start_char
                < run_end
            ):
                start_run_index = index

            if (
                run_start
                < end_char
                <= run_end
            ):
                end_run_index = index

        logger.debug(
            "[RUN RESOLUTION] "
            "start_run=%d "
            "end_run=%d",
            start_run_index,
            end_run_index,
        )

        if (
            start_run_index == -1
            or end_run_index == -1
        ):

            logger.debug(
                "[SPLICE FAIL] "
                "Could not map entity span "
                "to run boundaries. "
                "span=(%d,%d) "
                "total_run_length=%d",
                start_char,
                end_char,
                offset,
            )

            logger.debug(
                "[RUN SPANS] %s",
                [
                    (
                        index,
                        start,
                        end,
                    )
                    for (
                        index,
                        start,
                        end,
                        _,
                    ) in run_spans
                ],
            )

            return False

        # -------------------------------------------------
        # Entity contained inside one run.
        # -------------------------------------------------

        if (
            start_run_index
            == end_run_index
        ):

            (
                _,
                run_start,
                _,
                run,
            ) = run_spans[
                start_run_index
            ]

            relative_start = (
                start_char
                - run_start
            )

            relative_end = (
                end_char
                - run_start
            )

            text = run.text or ""

            logger.debug(
                "[SINGLE RUN] "
                "run=%d "
                "relative_span=(%d,%d) "
                "run_text=%r",
                start_run_index,
                relative_start,
                relative_end,
                text,
            )

            run.text = (
                text[:relative_start]
                + replacement
                + text[relative_end:]
            )

            logger.debug(
                "[SPLICE SUCCESS] "
                "single-run replacement"
            )

            return True

        # -------------------------------------------------
        # Entity spans multiple runs.
        # -------------------------------------------------

        (
            _,
            first_start,
            _,
            first_run,
        ) = run_spans[
            start_run_index
        ]

        relative_start = (
            start_char
            - first_start
        )

        first_text = (
            first_run.text or ""
        )

        logger.debug(
            "[MULTI RUN] "
            "first_run=%d "
            "relative_start=%d "
            "first_text=%r",
            start_run_index,
            relative_start,
            first_text,
        )

        first_run.text = (
            first_text[:relative_start]
            + replacement
        )

        # -------------------------------------------------
        # Clear intermediate runs.
        # -------------------------------------------------

        for index in range(
            start_run_index + 1,
            end_run_index,
        ):

            run = run_spans[
                index
            ][3]

            logger.debug(
                "[MULTI RUN] "
                "clearing run=%d "
                "text=%r",
                index,
                run.text,
            )

            run.text = ""

        # -------------------------------------------------
        # Preserve suffix of final run.
        # -------------------------------------------------

        (
            _,
            last_start,
            _,
            last_run,
        ) = run_spans[
            end_run_index
        ]

        relative_end = (
            end_char
            - last_start
        )

        last_text = (
            last_run.text or ""
        )

        logger.debug(
            "[MULTI RUN] "
            "last_run=%d "
            "relative_end=%d "
            "last_text=%r",
            end_run_index,
            relative_end,
            last_text,
        )

        last_run.text = last_text[
            relative_end:
        ]

        logger.debug(
            "[SPLICE SUCCESS] "
            "multi-run replacement"
        )

        return True

    def redact_paragraph(
        self,
        paragraph: Paragraph,
        entities: List[PiiEntity],
        location: str,
    ) -> List[RedactionAudit]:
        """Redact all detected entities in a paragraph."""

        if not entities:
            return []

        if not paragraph.runs:

            logger.warning(
                "[REDACT FAIL] "
                "Paragraph has entities "
                "but no runs: %s",
                location,
            )

            return []

        paragraph_text = (
            paragraph.text or ""
        )

        logger.debug(
            "\n[PARAGRAPH] location=%s",
            location,
        )

        logger.debug(
            "[PARAGRAPH] "
            "text_len=%d "
            "text=%r",
            len(paragraph_text),
            paragraph_text[:500],
        )

        logger.debug(
            "[PARAGRAPH] runs=%d",
            len(paragraph.runs),
        )

        # -------------------------------------------------
        # Log every detected entity before modification.
        # -------------------------------------------------

        for entity in entities:

            extracted = paragraph_text[
                entity.start_char:
                entity.end_char
            ]

            logger.debug(
                "[ENTITY] "
                "location=%s "
                "category=%s "
                "text=%r "
                "span=(%d,%d) "
                "span_text=%r "
                "matches=%s",
                location,
                entity.category,
                entity.text,
                entity.start_char,
                entity.end_char,
                extracted,
                extracted == entity.text,
            )

            if extracted != entity.text:

                logger.debug(
                    "[ENTITY MISMATCH] "
                    "expected=%r "
                    "actual=%r "
                    "location=%s",
                    entity.text,
                    extracted,
                    location,
                )

        # -------------------------------------------------
        # Process right-to-left so earlier offsets
        # remain valid.
        # -------------------------------------------------

        entities = sorted(
            entities,
            key=lambda entity: (
                entity.start_char,
                entity.end_char,
            ),
            reverse=True,
        )

        audits: List[
            RedactionAudit
        ] = []

        for entity in entities:

            replacement = (
                self.get_replacement(
                    entity
                )
            )

            logger.debug(
                "[REDACT] "
                "location=%s "
                "category=%s "
                "original=%r "
                "replacement=%r "
                "span=(%d,%d)",
                location,
                entity.category,
                entity.text,
                replacement,
                entity.start_char,
                entity.end_char,
            )

            success = self.splice_runs(
                paragraph,
                entity.start_char,
                entity.end_char,
                replacement,
            )

            if not success:

                logger.warning(
                    "[REDACT FAILED] "
                    "category=%s "
                    "entity=%r "
                    "location=%s "
                    "span=(%d,%d)",
                    entity.category,
                    entity.text,
                    location,
                    entity.start_char,
                    entity.end_char,
                )

                continue

            audits.append(
                RedactionAudit(
                    original=entity.text,
                    replacement=replacement,
                    category=entity.category,
                    location=location,
                )
            )

        return audits

    def redact_document(
        self,
        docx_path: str | Path,
        output_path: str | Path,
        detector: Optional[PiiDetector] = None,
        config_path: str | Path = "config.yaml",
    ) -> Tuple[
        int,
        List[RedactionAudit],
    ]:
        """Run detection and redaction on a DOCX document.

        Deduplicates paragraphs by underlying XML identity id(p._p) so that
        each paragraph is detected against original text and redacted exactly once.
        """

        extractor = DocxExtractor(
            docx_path
        )

        blocks = extractor.extract_all()

        if detector is None:

            detector = PiiDetector(
                config_path
            )

        # Deduplicate blocks by underlying paragraph XML identity
        unique_blocks = []
        seen_paragraphs: set[int] = set()
        for block in blocks:
            if block.raw_paragraph is None:
                continue
            p_id = id(block.raw_paragraph._p)
            if p_id in seen_paragraphs:
                continue
            seen_paragraphs.add(p_id)
            unique_blocks.append(block)

        texts = [
            block.text
            for block in unique_blocks
        ]

        logger.debug(
            "Starting detection across "
            "%d extracted blocks.",
            len(texts),
        )

        batch_entities = (
            detector.detect_batch(
                texts
            )
        )

        total_detected = sum(
            len(entities)
            for entities in batch_entities
        )

        logger.debug(
            "Detector returned %d entities "
            "across %d blocks.",
            total_detected,
            len(unique_blocks),
        )

        total_redactions = 0

        audits: List[
            RedactionAudit
        ] = []

        redacted_paragraphs: set[int] = set()

        for block, entities in zip(
            unique_blocks,
            batch_entities,
        ):

            if not entities:
                continue

            p_id = id(block.raw_paragraph._p)
            if p_id in redacted_paragraphs:
                continue
            redacted_paragraphs.add(p_id)

            logger.debug(
                "[BLOCK] "
                "location=%s "
                "category_count=%d "
                "text_len=%d",
                block.location,
                len(entities),
                len(block.text),
            )

            paragraph_audits = (
                self.redact_paragraph(
                    block.raw_paragraph,
                    entities,
                    block.location,
                )
            )

            total_redactions += len(
                paragraph_audits
            )

            audits.extend(
                paragraph_audits
            )

        output = Path(
            output_path
        )

        output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        extractor.doc.save(
            str(output)
        )

        logger.debug(
            "Redaction completed: "
            "%d replacements",
            total_redactions,
        )

        logger.debug(
            "Detection/redaction difference: "
            "detected=%d successful=%d failed=%d",
            total_detected,
            total_redactions,
            total_detected - total_redactions,
        )

        return (
            total_redactions,
            audits,
        )