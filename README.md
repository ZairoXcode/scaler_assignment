# PII Redaction & Anonymization

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://scalerassignment-pii-redaction.streamlit.app/)
> **Live Demo:** [https://scalerassignment-pii-redaction.streamlit.app/](https://scalerassignment-pii-redaction.streamlit.app/)

Python-based tool for detecting and redacting PII from Microsoft Word (`.docx`) documents. Built for the KSH International Limited Red Herring Prospectus assignment.

## Approach

The detector uses a hybrid approach:

* **Regex/rules** for email, phone, address, DOB, SSN, credit card and IP address.
* **spaCy NER** for person and organization names.
* **Document-specific rules** to reduce false positives in the RHP.

PANs, URLs, DINs, CINs, regulatory entities and similar document identifiers are recognized where needed for suppression, but are **not treated as PII categories for redaction**.

For organizations, the tool uses a simple policy:

| Organization type               | Action   |
| ------------------------------- | -------- |
| KSH / promoter-related entities | Redact   |
| Commercial intermediaries       | Preserve |
| Regulatory/statutory entities   | Preserve |

Examples of preserved entities include SEBI, BSE, NSE, RBI, MCA, Link Intime and Nuvama.

## Redaction

Detected entities are replaced with deterministic synthetic values generated using Faker. The same person or organization receives the same replacement throughout the document.

The DOCX processor reconstructs text for detection and maps detected spans back to Word runs. Only affected runs are modified, preserving the existing document structure and formatting as much as possible.

An OCR pass is also used for large embedded images. EasyOCR provides text and bounding boxes, and detected PII is visually redacted with blackout masks over the corresponding image regions rather than masking the complete image.

## Tradeoffs & Known Errors

The implementation uses deterministic synthetic replacement instead of simply masking detected text, so the resulting document remains readable while repeated entities receive consistent replacements. PAN, CIN, DIN and URLs are preserved rather than treated as PII because they are part of the regulatory/document context.

The main benchmark errors were caused by ambiguous boundaries: two organization names connected with "and" were detected as one entity, and an address immediately following a company name included part of the company name. These cases are documented in [`EVALUATION.md`](EVALUATION.md).

### Image / OCR Redaction & Accuracy

* **Visual Blackout vs. Synthetic Text Replacement (Not Yet Solved)**: While DOCX text paragraphs and tables use deterministic synthetic replacement to maintain readability, generating and rendering seamless synthetic text over raster ID card images (inpainting and matching scanned backgrounds/fonts) is **not yet solved**. Consequently, embedded image PII is currently redacted using solid blackout masking over detected regions rather than displaying synthetic replacement text.
* **Accuracy Expectations (< 100%)**: Image OCR does **not** claim 100% accuracy. Recognition quality depends heavily on image resolution, scanner contrast, skew, and compression artifacts in embedded raster ID cards. While standard printed fields (names, dates, ID numbers) are successfully localized and redacted, faint, low-contrast, or distorted text can be missed by EasyOCR. Consequently, image redaction functions as an automated best-effort visual masking pass with estimated ~85–90% coverage rather than guaranteed 100% recall.

## Project Structure

```text
extractor.py       DOCX text extraction
detector.py        PII detection and filtering
redactor.py        Synthetic replacement and DOCX redaction
ocr_processor.py   OCR and image-region redaction
evaluator.py       Benchmark evaluation
main.py            CLI entry point
streamlit_app.py   Streamlit web interface
tests/             Automated tests
```

## Run Locally

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Run the web application:

```bash
streamlit run streamlit_app.py
```

Run tests:

```bash
pytest -v
```

Run evaluation:

```bash
python main.py evaluate
```

Run redaction:

```bash
python main.py redact \
  --input "Red Herring Prospectus (3).docx" \
  --output "Redacted_Red_Herring_Prospectus.docx"
```

See [`EVALUATION.md`](EVALUATION.md) for the evaluation methodology, metrics, benchmark results, error analysis and full-document verification.

## Deliverables

* **Live Demo:** [https://scalerassignment-pii-redaction.streamlit.app/](https://scalerassignment-pii-redaction.streamlit.app/)
* Source code
* Redacted RHP (`.docx`)
* Streamlit web interface
* Evaluation report
* Automated tests
