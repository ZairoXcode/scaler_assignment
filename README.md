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

An OCR pass is also used for large embedded images. EasyOCR provides text and bounding boxes, and detected PII is redacted only within the corresponding image regions rather than masking the complete image.

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
