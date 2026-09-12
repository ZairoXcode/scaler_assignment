# KSH International RHP - PII Redaction & Anonymization Engine

Python-based tool for detecting and redacting PII from Microsoft Word (`.docx`) documents.

Built for the **KSH International Limited Red Herring Prospectus (RHP)** assignment. The tool detects the required PII, replaces it with synthetic values, and writes the result back to a DOCX while keeping the original document structure and formatting as much as possible.

## Submission Links

These are the links used for assignment review. Replace the three marked placeholders with the final public URLs before submitting:

- **Assignment:** KSH International Limited Red Herring Prospectus PII Redaction & Anonymization Engine
- **GitHub repository:** `YOUR_GITHUB_REPOSITORY_URL`
- **Live application (Render):** `YOUR_RENDER_SERVICE_URL`
- **Evaluation strategy and metrics:** [EVALUATION.md](EVALUATION.md)
- **Processed DOCX (public view access):** `YOUR_PUBLIC_DOCX_LINK`

The processed file is also included locally as [Redacted_Red_Herring_Prospectus.docx](Redacted_Red_Herring_Prospectus.docx). The public DOCX link should point to a share whose access is set to “Anyone with the link can view”.

## How it works

The detector uses a combination of regular expressions, document-specific rules and spaCy NER.

### Regex and rules

Structured PII is detected using patterns and rules for:

- Email addresses
- Phone numbers
- Social Security Numbers
- Credit card numbers
- IP addresses
- Dates of birth
- Physical/mailing addresses

### spaCy NER

spaCy is used for:

- Person names
- Company names

The detector also has rules for common corporate-document patterns. For example, PANs, URLs, DINs, CINs, order/reference numbers and regulatory entities are recognized where necessary to avoid false positives. These are preserved and are **not treated as PII for redaction**.

## Organization handling

Not every organization mentioned in the RHP should be replaced. The implementation uses a small policy for this:

| Type                       | Examples                                              | Action   |
| -------------------------- | ----------------------------------------------------- | -------- |
| Target / promoter entities | KSH International, promoter entities, relevant trusts | Redact   |
| Commercial intermediaries  | Link Intime, Nuvama, Kirtane & Pandit                 | Preserve |
| Regulatory / statutory     | SEBI, BSE, NSE, RBI, MCA, Companies Act               | Preserve |

This keeps the document's important regulatory and commercial context while replacing the organizations relevant to the assignment.

## Consistent replacements

Detected names and organizations are normalized before generating replacements.

If the same person or organization appears multiple times, it receives the same synthetic value throughout the document.

Synthetic values are generated with `Faker` using a fixed seed, making the output reproducible.

Example:

```text
Kushal Subbayya Hegde → Daniel Carter
original@email.com → emily.wilson@example.com
```

## DOCX handling

Word documents can split one sentence across multiple formatting runs. The detector therefore works on reconstructed text, while the redactor maps the detected spans back to the original runs.

For entities spanning multiple runs, only the affected runs are changed rather than rebuilding the document as plain text.

Redactions are applied from right to left so character positions for earlier matches remain valid.

## Evaluation

A 20-sample benchmark was created using RHP excerpts and synthetic cases for PII types that were not present in the source document.

| Metric                       |      Result |
| ---------------------------- | ----------: |
| Precision                    |  **94.59%** |
| Recall                       |  **92.11%** |
| F1                           |  **93.33%** |
| Sample-level accuracy        |  **90.00%** |
| Negative-control specificity | **100.00%** |

The benchmark's main errors were related to organization and address boundaries.

The benchmark is small, so these numbers represent performance on this test set rather than a general accuracy claim.

See [EVALUATION.md](EVALUATION.md) for the benchmark, error analysis, OCR image-redaction checks, and metric definitions.

## Full RHP result

The complete ~127-page RHP was processed successfully.

| Result                 |     Count |
| ---------------------- | --------: |
| Extracted blocks       |     4,486 |
| PII detected           |       522 |
| Successful redactions  |       522 |
| Failed redactions      |         0 |
| Verification residuals |         0 |
| Processing time        | 40.86 sec |

Detected entities:

```text
PERSON          311
ORGANIZATION    111
EMAIL            52
PHONE            36
ADDRESS          12
-------------------
TOTAL           522
```

All detected occurrences were successfully replaced, and the verification pass found no residual occurrences of those detected values.

SSN, credit card and IP address benchmark cases are synthetic because those types were not present in the provided RHP.

## Project structure

```text
extractor.py    DOCX text extraction
detector.py     PII detection and filtering
redactor.py     Synthetic replacement and DOCX redaction
evaluator.py    Benchmark evaluation
main.py         CLI entry point
app.py          FastAPI web interface
```

## Running locally

### Install

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

### Web app

The project includes a small FastAPI web interface for uploading a DOCX and downloading the redacted result.

```bash
uvicorn app:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

### Run tests

```bash
pytest -v
```

The repository contains the automated detection and redaction tests.

### Run evaluation

```bash
python main.py evaluate
```

The report is written to:

```text
data/output/evaluation_report.json
```

### Run redaction

```bash
python main.py redact \
  --input "Red Herring Prospectus (3).docx" \
  --output "Redacted_Red_Herring_Prospectus.docx"
```

## Known limitations

- Some organization and address boundaries can be ambiguous.
- Standalone geographic references are not redacted unless there is enough context to identify them as an address.
- OCR coverage depends on image quality and EasyOCR's reading of low-resolution or handwritten content.
- Some uncommon IPv6 formats are not covered.
- Text split across independent DOCX blocks, such as separate table cells, is processed independently.

## Deliverables

- Source code
- Web application (`app.py`, `templates/`, `render.yaml`)
- Redacted RHP
- Evaluation report
- Automated tests
- README
- `EVALUATION.md`
