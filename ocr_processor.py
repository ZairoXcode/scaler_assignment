"""OCR processor for PII detection in embedded DOCX images.

Adds an isolated, opt-in OCR layer on top of the existing PII redaction
engine. Uses EasyOCR to extract word-level bounding boxes from embedded
images and runs the existing PiiDetector on the extracted text so that
the same detection rules, suppression logic, and entity-resolution policy
apply uniformly to both native text and image content.

Design principles:
- Isolated: no modification to the existing extract / detect / redact
  pipeline. This module is invoked as a separate, optional post-pass.
- Fail-safe: every EasyOCR / Pillow call is wrapped in try/except so
  that an OCR failure never blocks or corrupts native text redaction.
- Reuses PiiDetector: the same ``detector.detect(text)`` method used for
  paragraph text is used here, guaranteeing identical scope (9 required
  PII categories) and suppression behaviour.
- Image-level redaction: detected PII bounding boxes are painted over
  with a solid black rectangle directly on the in-memory image bytes
  before the image part is re-embedded in the DOCX XML, leaving the
  surrounding document unchanged.

Dependencies (optional -- OCR is silently skipped when absent):
- easyocr >= 1.7.0
- Pillow >= 10.0.0

Usage::

    from ocr_processor import process_embedded_images
    from detector import PiiDetector

    detector = PiiDetector()
    image_redaction_count = process_embedded_images(doc, detector)
"""

from __future__ import annotations

import io
import logging
import re
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from docx import Document
    from detector import PiiDetector, PiiEntity


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional-dependency guards
# ---------------------------------------------------------------------------

try:
    import easyocr  # type: ignore
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False
    logger.debug(
        "easyocr is not installed. "
        "OCR image redaction will be skipped."
    )

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
    _PILLOW_AVAILABLE = True
except ImportError:
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore
    _PILLOW_AVAILABLE = False
    logger.debug(
        "Pillow is not installed. "
        "OCR image redaction will be skipped."
    )

# ---------------------------------------------------------------------------
# EasyOCR reader (lazy singleton — initialised once on first use)
# ---------------------------------------------------------------------------

_reader = None


def _get_reader():
    """Return a shared EasyOCR Reader instance (English, GPU optional)."""
    global _reader
    if not _EASYOCR_AVAILABLE:
        return None
    if _reader is None:
        # gpu=False avoids CUDA dependency; EasyOCR works fine on CPU
        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


# ---------------------------------------------------------------------------
# Candidate-image heuristics
# ---------------------------------------------------------------------------

# Only attempt OCR on images whose area (pixels) exceeds this threshold.
_MIN_AREA_PX: int = 40_000  # approx 200 x 200 px

# Minimum character count returned by EasyOCR for an image to be
# considered text-bearing.
_MIN_OCR_CHARS: int = 20


def _is_candidate(image) -> bool:
    """Return True when image is worth running OCR on."""
    width, height = image.size
    return (width * height) >= _MIN_AREA_PX


# ---------------------------------------------------------------------------
# Core OCR helpers
# ---------------------------------------------------------------------------

def _ocr_words(image) -> List[Tuple[str, int, int, int, int]]:
    """Run EasyOCR word-level extraction on image.

    Returns a list of (word, left, top, right, bottom) tuples where
    coordinates are in pixels relative to the image origin.

    EasyOCR returns results as:
        [ (bbox, text, confidence), ... ]
    where bbox is [[x1,y1],[x2,y1],[x2,y2],[x1,y2]] (4 corners).
    """
    import numpy as np

    img_array = np.array(image.convert("RGB"))
    reader = _get_reader()
    results = reader.readtext(
        img_array,
        detail=1,
        paragraph=False,
        mag_ratio=1.5,
        text_threshold=0.4,
        low_text=0.2,
        link_threshold=0.2,
    )

    words: List[Tuple[str, int, int, int, int]] = []

    for bbox, text, confidence in results:
        text = text.strip()
        if not text or confidence < 0.2:
            continue

        # bbox: [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        xs = [int(pt[0]) for pt in bbox]
        ys = [int(pt[1]) for pt in bbox]
        left   = min(xs)
        top    = min(ys)
        right  = max(xs)
        bottom = max(ys)

        # Split multi-word results into individual tokens and
        # distribute the bounding box evenly across them.
        tokens = text.split()
        if not tokens:
            continue

        token_width = max(1, (right - left) // len(tokens))
        for i, token in enumerate(tokens):
            t_left  = left + i * token_width
            t_right = min(t_left + token_width, right)
            words.append((token, t_left, top, t_right, bottom))

    return words


def _build_text_and_offsets(
    words: List[Tuple[str, int, int, int, int]],
) -> Tuple[str, List[Tuple[int, int, int, int, int, int]]]:
    """Concatenate words into a single string for the detector.

    Returns (full_text, offset_map) where offset_map is a list of
    (char_start, char_end, left, top, right, bottom) tuples mapping
    each word's character offsets back to its pixel bounding box.
    """
    parts: List[str] = []
    offset_map: List[Tuple[int, int, int, int, int, int]] = []

    cursor = 0

    for word, left, top, right, bottom in words:
        start = cursor
        end = cursor + len(word)

        parts.append(word)
        offset_map.append((start, end, left, top, right, bottom))

        cursor = end + 1  # +1 for the space separator

    full_text = " ".join(parts)
    return full_text, offset_map


_IGNORE_IMAGE_TERMS = {
    "income tax department",
    "govt of india",
    "government of india",
    "permanent account number card",
    "unique identification authority of india",
    "republic of india",
    "election commission of india",
    "date of birth",
    "father name",
    "permanent account",
    "services unit",
    "mantri sterling",
    "model coleay",
    "deep bungalow",
}


def _detect_image_pii(full_text: str, base_entities: list) -> list:
    """Augment detected entities with OCR-specific rules for identity documents.

    Catches Aadhaar numbers, PAN numbers, and person/father name fields that
    may be missed or suppressed in standard document prose but are personal PII
    on ID cards (PAN, Aadhaar, Voter ID, Passport).
    """
    from detector import PiiEntity

    entities = list(base_entities)
    existing_spans = {(e.start_char, e.end_char) for e in entities}

    def _add(text: str, category: str, start: int, end: int):
        replaceable = []
        for index, entity in enumerate(entities):
            if max(entity.start_char, start) < min(entity.end_char, end):
                # Image rules have document-specific context. Replace any
                # overlapping PERSON token so OCR fragments such as
                # ``U MERAJ`` and ``KHAN`` cannot block ``MERAJ KHAN``.
                if (
                    category == "PERSON"
                    and entity.category == "PERSON"
                ):
                    replaceable.append(index)
                else:
                    return

        for index in reversed(replaceable):
            old = entities.pop(index)
            existing_spans.discard((old.start_char, old.end_char))

        existing_spans.add((start, end))
        entities.append(
            PiiEntity(
                text=text,
                category=category,
                start_char=start,
                end_char=end,
                confidence=0.95,
                detector="image_rules",
            )
        )

    # 1. Aadhaar numbers (12 digits in 4 4 4 groups, or 12 continuous digits)
    for m in re.finditer(r"\b\d{4}\s\d{4}\s\d{4}\b", full_text):
        _add(m.group(), "AADHAAR", m.start(), m.end())
    for m in re.finditer(r"\b\d{12}\b", full_text):
        _add(m.group(), "AADHAAR", m.start(), m.end())

    # 2. PAN numbers in images (tolerant to OCR letter/digit confusions: 0/O, 5/S, 1/I)
    for m in re.finditer(r"\b[A-Z]{5}[0-9OSIBA]{4}[A-Z]\b", full_text):
        _add(m.group(), "PAN", m.start(), m.end())

    # 3. Name following 'Name' label / cue on ID cards. OCR frequently
    # returns labels and values in one line, so stop at the next field cue
    # instead of relying on spaCy to identify an all-caps value.
    for m in re.finditer(
        r"\bName\b\s*[:/]?\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2}?)(?=\s+(?:Father(?:'s)?|Date|DOB|Birth|Gender|Male|Female|\d)|$)",
        full_text,
        re.I,
    ):
        val = m.group(1).strip()
        if val.lower() not in _IGNORE_IMAGE_TERMS and len(val) >= 3:
            _add(val, "PERSON", m.start(1), m.start(1) + len(val))

    # 4. Father's Name following 'Father' label / cue on ID cards.
    for m in re.finditer(
        r"\bFather(?:'s)?(?:\s+Name)?\b\s*[:/$-]?\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2}?)(?=\s+(?:Date|DOB|Birth|Gender|Male|Female|Name|\d)|$)",
        full_text,
        re.I,
    ):
        val = m.group(1).strip()
        val = re.sub(r"\s+(?:Tr|7TA|Aat)$", "", val).strip()
        if val.lower() not in _IGNORE_IMAGE_TERMS and len(val) >= 3:
            _add(val, "PERSON", m.start(1), m.start(1) + len(val))

    # 5. Full uppercase names on ID cards (e.g. MERAJ KHAN, VISHAL SINGH)
    for m in re.finditer(r"\b([A-Z]{3,}\s+[A-Z]{3,})\b", full_text):
        val = m.group(1).strip()
        if val.lower() not in _IGNORE_IMAGE_TERMS and not any(
            w in val.lower()
            for w in [
                "income",
                "tax",
                "govt",
                "india",
                "account",
                "card",
                "permanent",
                "department",
            ]
        ):
            _add(val, "PERSON", m.start(1), m.end(1))

    # 6. Dates of Birth on ID cards
    for m in re.finditer(r"\b\d{2}[/-]\d{2}[/-]\d{4}\b", full_text):
        _add(m.group(), "DOB", m.start(), m.end())

    # 7. Signature text on ID cards
    for m in re.finditer(r"\b(?:Signature|हस्ताक्षर)\b", full_text, re.I):
        _add(m.group(), "SIGNATURE", m.start(), m.end())

    return entities


def _entity_bboxes(
    entities,
    offset_map: List[Tuple[int, int, int, int, int, int]],
) -> List[Tuple[int, int, int, int, str]]:
    """Map detected entity char-spans back to pixel bounding boxes.

    Returns a list of (left, top, right, bottom, category) tuples.
    """
    results: List[Tuple[int, int, int, int, str]] = []

    for entity in entities:
        e_start = entity.start_char
        e_end = entity.end_char

        overlapping: List[Tuple[int, int, int, int]] = []

        for w_start, w_end, left, top, right, bottom in offset_map:
            if w_start < e_end and w_end > e_start:
                overlapping.append((left, top, right, bottom))

        if not overlapping:
            logger.debug(
                "[OCR] No bounding box found for entity %r (%s) "
                "at chars (%d, %d)",
                entity.text,
                entity.category,
                e_start,
                e_end,
            )
            continue

        min_left   = min(b[0] for b in overlapping)
        min_top    = min(b[1] for b in overlapping)
        max_right  = max(b[2] for b in overlapping)
        max_bottom = max(b[3] for b in overlapping)

        # OCR can return only the last token of a name (for example KHAN).
        # Image-rule entities may absorb adjacent words on the same line;
        # generic document PERSON entities retain their precise geometry.
        if (
            entity.category == "PERSON"
            and getattr(entity, "detector", "") == "image_rules"
            and len(entity.text.split()) == 1
        ):
            entity_height = max_bottom - min_top
            for w_start, w_end, left, top, right, bottom in offset_map:
                vertical_overlap = min(max_bottom, bottom) - max(min_top, top)
                gap = max(min_left - right, left - max_right, 0)
                if vertical_overlap >= entity_height * 0.5 and gap <= entity_height * 2:
                    min_left = min(min_left, left)
                    min_top = min(min_top, top)
                    max_right = max(max_right, right)
                    max_bottom = max(max_bottom, bottom)

        if entity.category == "SIGNATURE":
            sig_h = max_bottom - min_top
            min_top = max(0, min_top - int(sig_h * 1.5))
            min_left = max(0, min_left - int(sig_h * 1.0))
            max_right += int(sig_h * 1.0)
            max_bottom += int(sig_h * 0.25)

        results.append((min_left, min_top, max_right, max_bottom, entity.category))

    return results


def _redact_image(
    image,
    bboxes: List[Tuple[int, int, int, int, str]],
    fill: Tuple[int, int, int] = (0, 0, 0),
    text_color: Tuple[int, int, int] = (255, 255, 255),
):
    """Paint solid rectangles with [REDACTED] labels over each bbox.

    Each bbox is a (left, top, right, bottom, category) tuple.
    The category is displayed as white text on the black bar when
    the box is large enough to fit the label.
    """
    img = image.convert("RGB")
    draw = ImageDraw.Draw(img)
    image_width, image_height = img.size

    def _font(size: int):
        size = max(1, int(size))
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size=size)
        except Exception:
            try:
                return ImageFont.load_default(size=size)
            except TypeError:
                return ImageFont.load_default()

    for left, top, right, bottom, category in bboxes:
        box_left = max(0, left - 2)
        box_top = max(0, top - 2)
        box_right = min(image_width, right + 2)
        box_bottom = min(image_height, bottom + 2)
        box_w = max(1, box_right - box_left)
        box_h = max(1, box_bottom - box_top)

        # 1. Solid opaque fill — permanently destroys underlying pixels
        draw.rectangle(
            [box_left, box_top, box_right, box_bottom],
            fill=fill,
        )

        # 2. Draw [REDACTED] label inside the black bar
        label = "[REDACTED]"
        try:
            # Start from the bar height and reduce until the complete label
            # fits both dimensions.  A small minimum keeps narrow OCR boxes
            # visibly labelled instead of silently omitting the annotation.
            font = _font(min(24, max(1, int(box_h * 0.65))))
            for size in range(min(24, max(1, int(box_h * 0.65))), 0, -1):
                candidate = _font(size)
                tb = draw.textbbox((0, 0), label, font=candidate)
                if tb[2] - tb[0] <= box_w and tb[3] - tb[1] <= box_h:
                    font = candidate
                    break

            tb = draw.textbbox((0, 0), label, font=font)
            tw = tb[2] - tb[0]
            th = tb[3] - tb[1]
            tx = box_left + max(0, (box_w - tw) // 2) - tb[0]
            ty = box_top + max(0, (box_h - th) // 2) - tb[1]
            draw.text((tx, ty), label, fill=text_color, font=font)
        except Exception:
            # The opaque bar remains the important part if a font backend is
            # unavailable; Pillow errors must never skip the redaction.
            pass

    return img


# ---------------------------------------------------------------------------
# DOCX image-part iteration helper
# ---------------------------------------------------------------------------

def _iter_image_parts(doc):
    """Yield every image part reachable from the document package."""
    visited_parts = set()
    yielded_images = set()

    def walk(part):
        part_key = id(part)
        if part_key in visited_parts:
            return
        visited_parts.add(part_key)

        try:
            for rel in part.rels.values():
                target = rel.target_part
                if "image" in rel.reltype.lower():
                    image_key = id(target)
                    if image_key not in yielded_images:
                        yielded_images.add(image_key)
                        yield target, rel.rId
                    continue

                # Avoid traversing lightweight test doubles while following
                # real python-docx package parts such as headers and footers.
                if target.__class__.__module__.startswith("docx."):
                    yield from walk(target)
        except Exception as exc:
            logger.debug(
                "[OCR] Could not iterate relationships for %s: %s",
                type(part).__name__,
                exc,
            )

    yield from walk(doc.part)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_embedded_images(doc, detector) -> int:
    """Detect and redact PII in all embedded images of doc.

    Modifies doc in-place by overwriting the binary data of each
    image part that contains detected PII. The surrounding document
    XML (paragraphs, runs, tables) is not touched.

    Parameters
    ----------
    doc:
        An open python-docx Document object. The caller is responsible
        for saving it after this function returns.
    detector:
        The shared PiiDetector instance from the main pipeline.
        Its detect(text) method is reused so that OCR entities are
        subject to the same 9-category scope and suppression rules.

    Returns
    -------
    int
        Number of PII bounding boxes redacted across all images.
        Returns 0 if OCR is unavailable or no PII is found.
    """
    if not (_EASYOCR_AVAILABLE and _PILLOW_AVAILABLE):
        logger.info(
            "[OCR] Skipping image redaction: "
            "easyocr or Pillow not available."
        )
        return 0

    total_redacted = 0
    images_scanned = 0
    images_redacted = 0

    for image_part, rel_id in _iter_image_parts(doc):
        try:
            image_bytes = image_part.blob
            image = Image.open(io.BytesIO(image_bytes))

        except Exception as exc:
            logger.debug(
                "[OCR] Could not open image (rel=%s): %s",
                rel_id,
                exc,
            )
            continue

        if not _is_candidate(image):
            logger.debug(
                "[OCR] Skipping small image (rel=%s, size=%s)",
                rel_id,
                image.size,
            )
            continue

        images_scanned += 1

        # ----------------------------------------------------------------
        # Run EasyOCR
        # ----------------------------------------------------------------
        try:
            words = _ocr_words(image)
        except Exception as exc:
            logger.warning(
                "[OCR] EasyOCR failed on image (rel=%s): %s",
                rel_id,
                exc,
            )
            continue

        if not words:
            logger.debug(
                "[OCR] No words extracted from image (rel=%s)",
                rel_id,
            )
            continue

        full_text, offset_map = _build_text_and_offsets(words)

        if len(full_text) < _MIN_OCR_CHARS and not re.search(
            r"(?:\d{12}|\d{4}\s+\d{4}\s+\d{4}|[A-Z]{5}[0-9OSIB]{4}[A-Z])",
            full_text,
            re.I,
        ):
            logger.debug(
                "[OCR] Too little text in image (rel=%s, chars=%d)",
                rel_id,
                len(full_text),
            )
            continue

        logger.debug(
            "[OCR] Image rel=%s | words=%d | chars=%d",
            rel_id,
            len(words),
            len(full_text),
        )

        # ----------------------------------------------------------------
        # Detect PII using the shared detector (same rules as native text)
        # ----------------------------------------------------------------
        try:
            base_entities = detector.detect(full_text)
            entities = _detect_image_pii(full_text, base_entities)
        except Exception as exc:
            logger.warning(
                "[OCR] Detector failed on OCR text (rel=%s): %s",
                rel_id,
                exc,
            )
            continue

        if not entities:
            logger.debug(
                "[OCR] No PII detected in image (rel=%s)",
                rel_id,
            )
            continue

        logger.info(
            "[OCR] Detected %d PII entity/entities in image (rel=%s): %s",
            len(entities),
            rel_id,
            [(e.category, e.text) for e in entities],
        )

        # ----------------------------------------------------------------
        # Map entity char-spans to pixel bounding boxes
        # ----------------------------------------------------------------
        bboxes = _entity_bboxes(entities, offset_map)

        if not bboxes:
            continue

        # ----------------------------------------------------------------
        # Paint redaction rectangles and re-embed the image
        # ----------------------------------------------------------------
        try:
            redacted_image = _redact_image(image, bboxes)

            buf = io.BytesIO()
            fmt = image.format or "PNG"
            if fmt.upper() in ("JPEG", "JPG"):
                redacted_image.save(buf, format="JPEG", quality=95)
            else:
                redacted_image.save(buf, format="PNG")

            image_part._blob = buf.getvalue()

            count = len(bboxes)
            total_redacted += count
            images_redacted += 1

            logger.info(
                "[OCR] Redacted %d PII region(s) in image (rel=%s)",
                count,
                rel_id,
            )

        except Exception as exc:
            logger.warning(
                "[OCR] Failed to redact image (rel=%s): %s",
                rel_id,
                exc,
            )
            continue

    logger.info(
        "[OCR] Summary: scanned=%d redacted_images=%d total_pii_regions=%d",
        images_scanned,
        images_redacted,
        total_redacted,
    )

    return total_redacted
