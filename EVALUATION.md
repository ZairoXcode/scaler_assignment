# Technical Evaluation

Evaluation of the PII detection and redaction tool built for the KSH International Limited Red Herring Prospectus (RHP).

## Submission Context

* **Assignment:** KSH International Limited Red Herring Prospectus PII Redaction & Anonymization Engine
* **GitHub repository:** `YOUR_GITHUB_REPOSITORY_URL`
* **Live application (Render):** `YOUR_RENDER_SERVICE_URL`
* **Processed DOCX:** `YOUR_PUBLIC_DOCX_LINK`

The local output used for verification is `Redacted_Red_Herring_Prospectus.docx`.

The links above are placeholders and should be replaced with the final submission links.

---

## 1. Evaluation Setup

The evaluation checks two parts:

1. Whether the detector identifies the required PII correctly.
2. Whether detected PII is replaced correctly in the DOCX.

The text benchmark contains **20 manually annotated samples**:

* 15 positive samples
* 5 negative controls

The benchmark covers the 9 PII categories required by the assignment:

* `PERSON`
* `ORGANIZATION`
* `EMAIL`
* `PHONE`
* `ADDRESS`
* `DOB`
* `SSN`
* `CREDIT_CARD`
* `IP_ADDRESS`

SSN, credit card and IP address examples are synthetic because those values were not found in the supplied RHP.

### Image/OCR evaluation

Image-based content is evaluated separately from the text benchmark.

The supplied RHP contains two large embedded ID-card images on page 127. The OCR processor uses EasyOCR to extract text and word-level bounding boxes. OCR output is then passed through the same PII detection logic used by the native text pipeline.

Detected OCR entities are mapped back to image coordinates and redacted locally rather than masking the entire image.

The image evaluation checks:

* OCR candidate detection
* OCR text extraction
* word bounding boxes
* PII span-to-image mapping
* localized visual redaction
* preservation of unrelated image content
* successful replacement of the embedded image in the DOCX

The image contains additional sensitive or identifying information such as PAN/Aadhaar numbers and signature areas. These are treated as **out-of-scope visual preservation/redaction cases** for this assignment and are not added as new benchmark PII categories.

### Negative controls

Negative controls include examples such as:

* SEBI/BSE/NSE references
* Companies Act citations
* financial dates
* PO/invoice/account numbers
* DINs
* insurance policy numbers
* PANs
* URLs

These should remain unchanged because they are not part of the required 9-category PII scope.

---

## 2. Metrics

The main detection metrics are precision, recall and F1.

```text
Precision = TP / (TP + FP)

Recall = TP / (TP + FN)

F1 = 2 × (Precision × Recall) / (Precision + Recall)
```

I also report:

* sample-level accuracy
* negative-control specificity

**Sample-level accuracy** is the percentage of benchmark samples with no false positives and no false negatives.

**Negative-control specificity** measures whether samples containing only non-PII content remain free of PII detections.

For span-based PII detection, entity-level accuracy is not used as the primary metric. Precision, recall and F1 provide a more useful measure of detection quality.

---

## 3. Benchmark Results

The evaluator uses category matching and normalized text comparison to account for minor formatting differences.

| Category         | TP | FP | FN | Precision |  Recall |      F1 |
| ---------------- | -: | -: | -: | --------: | ------: | ------: |
| **PERSON**       | 12 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| **ORGANIZATION** |  6 |  1 |  2 |    85.71% |  75.00% |  80.00% |
| **EMAIL**        |  5 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| **PHONE**        |  5 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| **ADDRESS**      |  2 |  1 |  1 |    66.67% |  66.67% |  66.67% |
| **DOB**          |  1 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| **SSN**          |  1 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| **CREDIT_CARD**  |  1 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| **IP_ADDRESS**   |  2 |  0 |  0 |   100.00% | 100.00% | 100.00% |

### Overall

* **TP:** 35
* **FP:** 2
* **FN:** 3
* **Precision:** **94.59%**
* **Recall:** **92.11%**
* **F1:** **93.33%**
* **Sample-level accuracy:** **90.00%** (18/20)
* **Negative-control specificity:** **100.00%** (5/5)

The benchmark errors are related to entity boundaries in organization and address detection.

---

## 4. Error Analysis

### `sample_04` — organization boundary

The expected result contains two separate organizations:

```text
Annapurna Family Trust

Kanchenjunga Family Trust
```

The detector returned them as one span:

```text
Annapurna Family Trust and Kanchenjunga Family Trust
```

This produced one false positive and two false negatives in the benchmark scoring.

The issue is related to separating adjacent organization entities connected by natural language.

### `sample_13` — address boundary

The expected address was:

```text
C-101, 247 Park, L.B.S. Marg, Vikhroli (West), Mumbai 400 083
```

The detector included the preceding registrar name:

```text
Link Intime India Private Limited, C-101, 247 Park, ...
```

The company name and address appear next to each other without a strong delimiter, which makes the boundary ambiguous.

The structured detectors for email, phone, SSN, credit card, IP address and DOB had no false positives or false negatives in this benchmark.

---

## 5. Full RHP Run

The complete 127-page RHP was processed locally.

| Result                   |         Count |
| ------------------------ | ------------: |
| Extracted blocks         |         4,486 |
| PII occurrences detected |           522 |
| Successful redactions    |           522 |
| Failed redactions        |             0 |
| Verification residuals   |             0 |
| Processing time          | 40.86 seconds |

### Detected entities

```text
PERSON          311
ORGANIZATION    111
EMAIL            52
PHONE            36
ADDRESS          12
-------------------
TOTAL           522
```

The 522 detected occurrences were successfully replaced, and the verification pass found no remaining occurrences of those detected values in the output.

This verifies the redaction and DOCX run-handling for detected entities. It does **not** establish 100% recall for every possible PII reference in the document.

### Image/OCR result

The image post-pass processed the large embedded images in the RHP and produced **14 visual redaction regions**.

The visual regions covered successfully recognized sensitive content including:

* names
* dates of birth
* phone numbers
* addresses
* email content
* identifier/signature areas where applicable

These image results are reported separately because image PII does not exist as replaceable DOCX text runs.

The image output was also manually reviewed to confirm that redaction was localized to the detected regions and that the surrounding image content remained intact.

---

## 6. Reproducing the Results

Install the dependencies:

```bash
pip install -r requirements.txt
```

Install the spaCy model:

```bash
python -m spacy download en_core_web_sm
```

The OCR implementation also requires EasyOCR and its runtime dependencies as specified in `requirements.txt`.

Run the tests:

```bash
pytest -v
```

Run the benchmark:

```bash
python main.py evaluate
```

Run the full document redaction:

```bash
python main.py redact \
  --input "Red Herring Prospectus (3).docx" \
  --output "Redacted_Red_Herring_Prospectus.docx"
```

The benchmark report is saved to:

```text
data/output/evaluation_report.json
```

---

## 7. Limitations

* **Organization boundaries:** Two organizations connected with "and" can sometimes be returned as one span.
* **Address boundaries:** An address immediately following a company name can sometimes include part of the company name.
* **OCR:** OCR quality depends on image resolution, contrast, language, layout and recognition quality. Handwritten or distorted content may be less reliable.
* **Image layouts:** The OCR path is intended primarily for printed text in embedded raster images. It is not a general-purpose handwritten document recognition system.
* **DOCX structure:** Text split across independent DOCX blocks, such as separate table cells, is processed independently.
* **IPv6:** Common IPv6 formats are supported, but some uncommon formats may not be detected.
* **Image benchmark:** OCR/image results are evaluated separately from the 20-sample text benchmark and are not combined with the reported 94.59% precision and 92.11% recall.

The reported metrics should therefore be interpreted as measurements of the tested benchmark, not as a guarantee of perfect detection across arbitrary documents.
