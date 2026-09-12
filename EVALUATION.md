# Technical Evaluation

Evaluation of the PII detection and redaction tool against the KSH International Limited Red Herring Prospectus (RHP).

## 1. Evaluation Setup

The evaluation covers the 9 PII categories specified in the assignment:

* `PERSON`
* `ORGANIZATION`
* `EMAIL`
* `PHONE`
* `ADDRESS`
* `DOB`
* `SSN`
* `CREDIT_CARD`
* `IP_ADDRESS`

The text benchmark contains **20 manually annotated samples**:

* 15 positive samples
* 5 negative controls

SSN, credit card and IP address examples are synthetic because those values were not present in the supplied RHP.

PANs, URLs, DINs, CINs, regulatory references and similar document identifiers were used as negative controls. They are intentionally preserved and are not part of the required PII scope.

## 2. Metrics

```text
Precision = TP / (TP + FP)

Recall = TP / (TP + FN)

F1 = 2 × (Precision × Recall) / (Precision + Recall)
```

Sample-level accuracy is the percentage of benchmark samples with no false positive or false negative.

Negative-control specificity measures whether non-PII samples remain free of PII detections.

## 3. Benchmark Results

| Category     | TP | FP | FN | Precision |  Recall |      F1 |
| ------------ | -: | -: | -: | --------: | ------: | ------: |
| PERSON       | 12 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| ORGANIZATION |  6 |  1 |  2 |    85.71% |  75.00% |  80.00% |
| EMAIL        |  5 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| PHONE        |  5 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| ADDRESS      |  2 |  1 |  1 |    66.67% |  66.67% |  66.67% |
| DOB          |  1 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| SSN          |  1 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| CREDIT_CARD  |  1 |  0 |  0 |   100.00% | 100.00% | 100.00% |
| IP_ADDRESS   |  2 |  0 |  0 |   100.00% | 100.00% | 100.00% |

### Overall

* **TP:** 35
* **FP:** 2
* **FN:** 3
* **Precision:** **94.59%**
* **Recall:** **92.11%**
* **F1:** **93.33%**
* **Sample-level accuracy:** **90.00% (18/20)**
* **Negative-control specificity:** **100.00% (5/5)**

## 4. Error Analysis

### Organization boundary

One sample contained two organizations connected by `and`:

```text
Annapurna Family Trust
Kanchenjunga Family Trust
```

The detector returned them as one span:

```text
Annapurna Family Trust and Kanchenjunga Family Trust
```

This caused the organization false positive/false negative counts in the benchmark.

### Address boundary

One expected address was:

```text
C-101, 247 Park, L.B.S. Marg, Vikhroli (West), Mumbai 400 083
```

The detector included the preceding company name:

```text
Link Intime India Private Limited, C-101, 247 Park, ...
```

The company and address occur next to each other, making the boundary ambiguous.

The structured detectors for email, phone, DOB, SSN, credit card and IP address had no errors in this benchmark.

## 5. Full RHP Verification

The complete **127-page RHP** was processed locally.

| Result                   |         Count |
| ------------------------ | ------------: |
| Extracted blocks         |         4,486 |
| PII occurrences detected |           522 |
| Successful redactions    |           522 |
| Failed redactions        |             0 |
| Verification residuals   |             0 |
| Processing time          | 40.86 seconds |

Detected entities:

```text
PERSON          311
ORGANIZATION    111
EMAIL            52
PHONE            36
ADDRESS          12
-------------------
TOTAL            522
```

All 522 detected occurrences were successfully replaced. The verification pass found no remaining occurrences of those detected values in the output.

This verifies the redaction pipeline for detected entities; it does **not** imply 100% recall across every possible PII instance.

## 6. Image / OCR Verification

The RHP contains large embedded ID-card images on page 127.

The OCR pipeline:

1. extracts text and word-level bounding boxes using EasyOCR;
2. passes OCR text through the existing PII detector;
3. maps detected spans to image coordinates;
4. redacts only the detected regions;
5. replaces the modified image inside the DOCX.

The image post-pass produced **14 visual redaction regions** covering recognized names, DOBs, phone numbers, addresses, email content and applicable identifier/signature regions.

The result was manually checked to confirm that redaction was localized and surrounding image content remained intact.

OCR results are reported separately and are not combined with the text benchmark metrics above.

## 7. Reproducing the Evaluation

Install dependencies:

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Run tests:

```bash
pytest -v
```

Run the benchmark:

```bash
python main.py evaluate
```

Run the full RHP redaction:

```bash
python main.py redact \
  --input "Red Herring Prospectus (3).docx" \
  --output "Redacted_Red_Herring_Prospectus.docx"
```

The machine-readable evaluation result is written to:

```text
data/output/evaluation_report.json
```

## 8. Limitations

* Organization boundaries can be ambiguous when multiple entities are connected by natural language.
* Address detection can occasionally include an adjacent company name.
* OCR accuracy depends on image quality, layout and recognition quality.
* The OCR path is intended for printed text in embedded images rather than general handwritten-document recognition.
* Text in independent DOCX blocks, such as separate table cells, is processed independently.
* Some uncommon IPv6 formats may not be detected.

The reported metrics describe the tested benchmark and should not be interpreted as a guarantee of perfect detection on arbitrary documents.
