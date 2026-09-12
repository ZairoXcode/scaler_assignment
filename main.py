"""CLI entrypoint for PII redaction and benchmark evaluation."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re

from detector import PiiDetector
from evaluator import run_evaluation
from extractor import DocxExtractor
from ocr_processor import process_embedded_images
from redactor import PiiRedactor


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )


logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
)


@dataclass
class PiiOccurrence:
    """Represents a detected PII occurrence tied to its specific paragraph block."""
    category: str
    original_text: str
    location: str
    paragraph_id: int


def detect_original_pii(
    input_docx_path: str | Path,
    detector: PiiDetector,
) -> list[PiiOccurrence]:
    """Collect original detected PII occurrences from unique DOCX paragraphs."""

    extractor = DocxExtractor(input_docx_path)
    blocks = extractor.extract_all()

    occurrences: list[PiiOccurrence] = []
    seen_paragraphs: set[int] = set()

    for block in blocks:
        if block.raw_paragraph is None:
            continue

        paragraph_id = id(block.raw_paragraph._p)

        if paragraph_id in seen_paragraphs:
            continue

        seen_paragraphs.add(paragraph_id)

        # Use the original paragraph text directly.
        text = block.raw_paragraph.text or ""

        entities = detector.detect(text)

        for entity in entities:
            value = entity.text.strip()

            if value:
                occurrences.append(
                    PiiOccurrence(
                        category=entity.category,
                        original_text=value,
                        location=block.location,
                        paragraph_id=paragraph_id,
                    )
                )

    return occurrences


def _count_matches(val: str, text: str) -> int:
    """Count occurrences of val in text, using word boundaries for alphanumeric terms."""
    esc = re.escape(val)
    pat = (
        r"(?i)\b" + esc + r"\b"
        if re.match(r"^\w", val) and re.match(r".*\w$", val)
        else r"(?i)" + esc
    )
    return len(re.findall(pat, text))


def post_redaction_scan(
    input_docx_path: str | Path,
    output_docx_path: str | Path,
    occurrences: list[PiiOccurrence],
) -> bool:
    """Verify at the occurrence level that detected PII was successfully removed from its paragraph."""

    print(
        "[*] Running post-redaction verification scan..."
    )

    in_extractor = DocxExtractor(input_docx_path)
    in_blocks = in_extractor.extract_all()
    in_text_by_loc = {b.location: b.text for b in in_blocks}

    out_extractor = DocxExtractor(output_docx_path)
    out_blocks = out_extractor.extract_all()
    out_text_by_loc = {b.location: b.text for b in out_blocks}

    # Group detected occurrences by location
    loc_occurrences: dict[str, list[PiiOccurrence]] = {}
    for occ in occurrences:
        loc_occurrences.setdefault(occ.location, []).append(occ)

    residuals: list[tuple[str, str, str]] = []

    for loc, occs in loc_occurrences.items():
        orig_text = in_text_by_loc.get(loc, "")
        red_text = out_text_by_loc.get(loc, "")

        # Group by unique entity text within this block
        counts = Counter(o.original_text for o in occs)
        for val, detected_count in counts.items():
            orig_c = _count_matches(val, orig_text)
            red_c = _count_matches(val, red_text)
            removed = orig_c - red_c
            if removed < detected_count and red_c > 0:
                cat = next(o.category for o in occs if o.original_text == val)
                residuals.append((loc, cat, val))

    if not residuals:
        print(
            "    [+] Verification passed: "
            "all detected PII occurrences were successfully removed."
        )
        return True

    print(
        f"    [!] Verification found "
        f"{len(residuals)} residual PII occurrence(s):"
    )

    for loc, cat, val in residuals[:10]:
        print(f"        - [{loc}] {cat}: {val}")

    if len(residuals) > 10:
        print(
            f"        ... and {len(residuals) - 10} more"
        )

    return False


def run_redaction(args) -> None:
    """Run the complete redaction workflow."""

    print(
        "\n======================================================="
    )
    print(
        "             PII REDACTION ENGINE"
    )
    print(
        "=======================================================\n"
    )

    start = time.time()

    print(f"[*] Input DOCX : {args.input}")
    print(f"[*] Output DOCX: {args.output}")
    print(f"[*] Config     : {args.config}")

    input_path = Path(args.input)

    if not input_path.exists():
        raise FileNotFoundError(
            f"Input DOCX not found: {args.input}"
        )

    detector = PiiDetector(
        args.config
    )

    redactor = PiiRedactor(
        args.config
    )

    # Detect original PII before modifying the document.
    print(
        "[*] Detecting original PII for "
        "post-redaction verification..."
    )

    original_pii = detect_original_pii(
        input_path,
        detector,
    )

    print(
        f"[*] Detected {len(original_pii)} "
        "PII occurrences."
    )

    # Perform redaction.
    print(
        "[*] Extracting and redacting PII "
        "across paragraphs and tables..."
    )

    count, audits = redactor.redact_document(
        docx_path=args.input,
        output_path=args.output,
        detector=detector,
        config_path=args.config,
    )

    elapsed = time.time() - start

    print(
        f"\n[+] Successfully applied "
        f"{count} redactions."
    )

    print(
        f"[+] Saved redacted DOCX to: "
        f"{args.output}"
    )

    # ------------------------------------------------------------------
    # OCR post-pass: detect and redact PII inside embedded images.
    # This is isolated from the native text pipeline; any failure is
    # logged but never blocks or alters the native redaction output.
    # ------------------------------------------------------------------
    try:
        import docx as _docx  # local import to avoid circular issues
        _ocr_doc = _docx.Document(str(Path(args.output)))
        ocr_count = process_embedded_images(_ocr_doc, detector)
        if ocr_count > 0:
            _ocr_doc.save(str(Path(args.output)))
            print(
                f"[+] OCR: redacted {ocr_count} PII region(s) "
                "in embedded image(s)."
            )
        else:
            print(
                "[*] OCR: no PII found in embedded images "
                "(or OCR unavailable)."
            )
    except Exception as _ocr_exc:
        print(
            f"[!] OCR post-pass skipped: {_ocr_exc}"
        )

    print(
        f"[+] Total processing time: "
        f"{elapsed:.2f} seconds"
    )

    # Verification.
    verification_passed = post_redaction_scan(
        args.input,
        args.output,
        original_pii,
    )

    print()

    if verification_passed:
        print(
            "[+] Final status: REDACTION VERIFIED"
        )
    else:
        print(
            "[!] Final status: REVIEW REQUIRED"
        )



def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "PII Redaction Engine for "
            "Regulatory Prospectus Documents"
        )
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help="Command to execute",
    )

    # ---------------------------------------------------------
    # redact
    # ---------------------------------------------------------
    redact_parser = subparsers.add_parser(
        "redact",
        help="Redact PII from a DOCX document",
    )

    redact_parser.add_argument(
        "--input",
        "-i",
        default="Red Herring Prospectus (3).docx",
        help="Path to input DOCX",
    )

    redact_parser.add_argument(
        "--output",
        "-o",
        default="Redacted_Red_Herring_Prospectus.docx",
        help="Path to output DOCX",
    )

    redact_parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="Path to configuration file",
    )

    # ---------------------------------------------------------
    # evaluate
    # ---------------------------------------------------------
    eval_parser = subparsers.add_parser(
        "evaluate",
        help="Run benchmark evaluation",
    )

    eval_parser.add_argument(
        "--sample",
        "-s",
        default="ground_truth_sample.json",
        help="Path to benchmark JSON",
    )

    eval_parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="Path to configuration file",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "evaluate":
            run_evaluation(
                sample_path=args.sample,
                config_path=args.config,
            )

        elif args.command == "redact":
            run_redaction(args)

    except FileNotFoundError as exc:
        print(
            f"[ERROR] {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    except Exception as exc:
        print(
            f"[ERROR] {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
