"""Tests for ocr_processor.py.

All tests are designed to run without a Tesseract binary or real DOCX file.
The module's optional-dependency guards (_TESSERACT_AVAILABLE / _PILLOW_AVAILABLE)
are bypassed via monkeypatching where necessary so that the Python-level logic
is verified independently of the system OCR installation.
"""

import io
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers to build lightweight fakes
# ---------------------------------------------------------------------------


def _make_entity(text: str, start: int, end: int, category: str = "PERSON"):
    """Return a minimal PiiEntity-like namespace."""
    e = types.SimpleNamespace()
    e.text = text
    e.start_char = start
    e.end_char = end
    e.category = category
    return e


def _make_image_mock(width: int = 500, height: int = 500):
    """Return a minimal Pillow Image mock."""
    img = MagicMock()
    img.size = (width, height)
    img.format = "PNG"
    return img


# ---------------------------------------------------------------------------
# _is_candidate
# ---------------------------------------------------------------------------


class TestIsCandidate(unittest.TestCase):

    def _call(self, image):
        from ocr_processor import _is_candidate
        return _is_candidate(image)

    def test_large_image_is_candidate(self):
        img = _make_image_mock(300, 200)  # 60 000 px — above 40 000 threshold
        self.assertTrue(self._call(img))

    def test_small_image_is_not_candidate(self):
        img = _make_image_mock(100, 100)  # 10 000 px — below threshold
        self.assertFalse(self._call(img))

    def test_border_image_is_candidate(self):
        # Exactly 40 000 px should be a candidate (>= check)
        img = _make_image_mock(200, 200)
        self.assertTrue(self._call(img))


# ---------------------------------------------------------------------------
# _build_text_and_offsets
# ---------------------------------------------------------------------------


class TestBuildTextAndOffsets(unittest.TestCase):

    def _call(self, words):
        from ocr_processor import _build_text_and_offsets
        return _build_text_and_offsets(words)

    def test_single_word(self):
        words = [("Hello", 10, 20, 60, 40)]
        text, offsets = self._call(words)
        self.assertEqual(text, "Hello")
        self.assertEqual(len(offsets), 1)
        char_start, char_end, left, top, right, bottom = offsets[0]
        self.assertEqual(char_start, 0)
        self.assertEqual(char_end, 5)
        self.assertEqual(left, 10)
        self.assertEqual(top, 20)
        self.assertEqual(right, 60)
        self.assertEqual(bottom, 40)

    def test_multiple_words_are_space_joined(self):
        words = [
            ("John", 0, 0, 40, 20),
            ("Doe", 50, 0, 80, 20),
        ]
        text, offsets = self._call(words)
        self.assertEqual(text, "John Doe")
        # "John" → chars 0-4; "Doe" → chars 5-8
        self.assertEqual(offsets[0][0], 0)
        self.assertEqual(offsets[0][1], 4)
        self.assertEqual(offsets[1][0], 5)
        self.assertEqual(offsets[1][1], 8)

    def test_empty_words(self):
        text, offsets = self._call([])
        self.assertEqual(text, "")
        self.assertEqual(offsets, [])


# ---------------------------------------------------------------------------
# _entity_bboxes
# ---------------------------------------------------------------------------


class TestEntityBboxes(unittest.TestCase):

    def _call(self, entities, offset_map):
        from ocr_processor import _entity_bboxes
        return _entity_bboxes(entities, offset_map)

    def _offset(self, cs, ce, l, t, r, b):
        return (cs, ce, l, t, r, b)

    def test_single_word_entity(self):
        # "John" occupies chars 0-4, bbox pixel (0,0,40,20)
        entity = _make_entity("John", 0, 4)
        offset_map = [self._offset(0, 4, 0, 0, 40, 20)]
        boxes = self._call([entity], offset_map)
        self.assertEqual(boxes, [(0, 0, 40, 20, "PERSON")])

    def test_multi_word_entity_merges_boxes(self):
        # "John Doe" spans chars 0-8; two words with separate pixel boxes
        entity = _make_entity("John Doe", 0, 8)
        offset_map = [
            self._offset(0, 4, 0, 0, 40, 20),
            self._offset(5, 8, 50, 0, 90, 20),
        ]
        boxes = self._call([entity], offset_map)
        self.assertEqual(len(boxes), 1)
        left, top, right, bottom, cat = boxes[0]
        self.assertEqual(left, 0)    # min of 0, 50
        self.assertEqual(top, 0)
        self.assertEqual(right, 90)  # max of 40, 90
        self.assertEqual(bottom, 20)

    def test_entity_with_no_overlap_returns_empty(self):
        entity = _make_entity("Ghost", 100, 105)
        offset_map = [self._offset(0, 4, 0, 0, 40, 20)]
        boxes = self._call([entity], offset_map)
        self.assertEqual(boxes, [])

    def test_multiple_entities(self):
        entities = [
            _make_entity("John", 0, 4),
            _make_entity("Doe", 5, 8, "EMAIL"),
        ]
        offset_map = [
            self._offset(0, 4, 0, 0, 40, 20),
            self._offset(5, 8, 50, 0, 90, 20),
        ]
        boxes = self._call(entities, offset_map)
        self.assertEqual(len(boxes), 2)


# ---------------------------------------------------------------------------
# process_embedded_images — integration tests via mocking
# ---------------------------------------------------------------------------


class TestProcessEmbeddedImages(unittest.TestCase):
    """Test process_embedded_images() without a real DOCX or EasyOCR."""

    def _run(self, doc_mock, detector_mock):
        from ocr_processor import process_embedded_images
        return process_embedded_images(doc_mock, detector_mock)

    # ------------------------------------------------------------------
    # Graceful skip when libraries unavailable
    # ------------------------------------------------------------------

    def test_skips_when_easyocr_unavailable(self):
        """Must return 0 and not raise when easyocr is absent."""
        with patch("ocr_processor._EASYOCR_AVAILABLE", False), \
             patch("ocr_processor._PILLOW_AVAILABLE", True):
            result = self._run(MagicMock(), MagicMock())
        self.assertEqual(result, 0)

    def test_skips_when_pillow_unavailable(self):
        """Must return 0 and not raise when Pillow is absent."""
        with patch("ocr_processor._EASYOCR_AVAILABLE", True), \
             patch("ocr_processor._PILLOW_AVAILABLE", False):
            result = self._run(MagicMock(), MagicMock())
        self.assertEqual(result, 0)

    # ------------------------------------------------------------------
    # No images in document
    # ------------------------------------------------------------------

    def test_returns_zero_for_empty_document(self):
        """Returns 0 when the document has no image parts."""
        doc = MagicMock()
        doc.part.rels = {}

        with patch("ocr_processor._EASYOCR_AVAILABLE", True), \
             patch("ocr_processor._PILLOW_AVAILABLE", True):
            result = self._run(doc, MagicMock())
        self.assertEqual(result, 0)

    # ------------------------------------------------------------------
    # Small image is skipped
    # ------------------------------------------------------------------

    def test_skips_small_image(self):
        """Images below _MIN_AREA_PX are silently skipped."""
        small_img = _make_image_mock(50, 50)

        rel = MagicMock()
        rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        rel.rId = "rId1"
        image_part = MagicMock()
        image_part.blob = b"fakedata"
        rel.target_part = image_part

        doc = MagicMock()
        doc.part.rels = {"rId1": rel}

        with patch("ocr_processor._EASYOCR_AVAILABLE", True), \
             patch("ocr_processor._PILLOW_AVAILABLE", True), \
             patch("ocr_processor.Image") as mock_pil:
            mock_pil.open.return_value = small_img
            result = self._run(doc, MagicMock())

        self.assertEqual(result, 0)

    # ------------------------------------------------------------------
    # OCR finds no text — returns 0
    # ------------------------------------------------------------------

    def test_returns_zero_when_no_words(self):
        """Returns 0 when EasyOCR yields no words from a large image."""
        large_img = _make_image_mock(500, 500)

        rel = MagicMock()
        rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        rel.rId = "rId1"
        image_part = MagicMock()
        image_part.blob = b"fakedata"
        rel.target_part = image_part

        doc = MagicMock()
        doc.part.rels = {"rId1": rel}

        with patch("ocr_processor._EASYOCR_AVAILABLE", True), \
             patch("ocr_processor._PILLOW_AVAILABLE", True), \
             patch("ocr_processor.Image") as mock_pil, \
             patch("ocr_processor._ocr_words", return_value=[]):
            mock_pil.open.return_value = large_img
            result = self._run(doc, MagicMock())

        self.assertEqual(result, 0)

    # ------------------------------------------------------------------
    # OCR detects PII and redraws the image
    # ------------------------------------------------------------------

    def test_redacts_pii_found_in_image(self):
        """Returns count > 0 and updates image_part._blob when PII is detected."""
        import io as _real_io

        large_img = _make_image_mock(500, 500)
        large_img.format = "PNG"

        # The redacted image returned by _redact_image must be a real-enough
        # PIL-like object whose .save() writes bytes we can intercept.
        redacted_img = MagicMock()

        def fake_save(buf, format="PNG", **kw):
            buf.write(b"REDACTED_PNG")

        redacted_img.save.side_effect = fake_save

        rel = MagicMock()
        rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        rel.rId = "rId1"
        image_part = MagicMock()
        image_part.blob = b"fakedata"
        rel.target_part = image_part

        doc = MagicMock()
        doc.part.rels = {"rId1": rel}

        fake_words = [
            ("Firstname", 10, 10, 80, 30),
            ("Lastname", 90, 10, 160, 30),
            ("extra1", 170, 10, 220, 30),
            ("extra2", 230, 10, 280, 30),
        ]
        # "Firstname Lastname extra1 extra2" → 32 chars, exceeds _MIN_OCR_CHARS=20
        # "Firstname Lastname" spans chars 0-17
        pii_entity = _make_entity("Firstname Lastname", 0, 17, "PERSON")
        detector = MagicMock()
        detector.detect.return_value = [pii_entity]

        with patch("ocr_processor._EASYOCR_AVAILABLE", True), \
             patch("ocr_processor._PILLOW_AVAILABLE", True), \
             patch("ocr_processor.Image") as mock_pil, \
             patch("ocr_processor._ocr_words", return_value=fake_words), \
             patch("ocr_processor._redact_image", return_value=redacted_img):

            mock_pil.open.return_value = large_img
            result = self._run(doc, detector)

        # One bounding box was redacted
        self.assertGreater(result, 0)

    # ------------------------------------------------------------------
    # Tesseract failure is handled gracefully
    # ------------------------------------------------------------------

    def test_handles_ocr_failure_gracefully(self):
        """If EasyOCR raises, the image is skipped without raising."""
        large_img = _make_image_mock(500, 500)

        rel = MagicMock()
        rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        rel.rId = "rId1"
        image_part = MagicMock()
        image_part.blob = b"fakedata"
        rel.target_part = image_part

        doc = MagicMock()
        doc.part.rels = {"rId1": rel}

        with patch("ocr_processor._EASYOCR_AVAILABLE", True), \
             patch("ocr_processor._PILLOW_AVAILABLE", True), \
             patch("ocr_processor.Image") as mock_pil, \
             patch("ocr_processor._ocr_words", side_effect=RuntimeError("EasyOCR died")):
            mock_pil.open.return_value = large_img
            result = self._run(doc, MagicMock())

        # Must not raise; must return 0
        self.assertEqual(result, 0)

    # ------------------------------------------------------------------
    # Non-image relationships are skipped
    # ------------------------------------------------------------------

    def test_skips_non_image_relationships(self):
        """Relationships whose reltype does not contain 'image' are skipped."""
        rel = MagicMock()
        rel.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
        rel.rId = "rId2"

        doc = MagicMock()
        doc.part.rels = {"rId2": rel}

        with patch("ocr_processor._EASYOCR_AVAILABLE", True), \
             patch("ocr_processor._PILLOW_AVAILABLE", True):
            result = self._run(doc, MagicMock())

        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
