"""Streamlit web interface for PII Redaction & Anonymization Engine."""

import gc
import io
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import docx
import streamlit as st

from detector import PiiDetector
from redactor import PiiRedactor

# -----------------------------------------------------------------------------
# Page Configuration & Styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="PII Redaction & Anonymization Engine",
    page_icon="🔒",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1e293b;
        margin-bottom: 0.25rem;
    }
    .sub-header {
        font-size: 1.05rem;
        color: #64748b;
        margin-bottom: 1.5rem;
    }
    .metric-box {
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 1rem;
        text-align: center;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">🔒 PII Redaction & Anonymization</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">Upload a Microsoft Word (.docx) document to detect and redact sensitive PII across text runs, tables, and embedded scanned ID cards.</div>',
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Model Pre-warming with Caching
# -----------------------------------------------------------------------------
@st.cache_resource(show_spinner="Initializing PII detection engine (spaCy & rules)...")
def get_engine():
    """Ensure spaCy model is installed and return cached detector & redactor instances."""
    try:
        import spacy
        spacy.load("en_core_web_sm")
    except Exception:
        try:
            from spacy.cli import download
            download("en_core_web_sm")
        except Exception:
            subprocess.run(
                [sys.executable, "-m", "spacy", "download", "en_core_web_sm"],
                check=True,
            )

    detector = PiiDetector("config.yaml")
    redactor = PiiRedactor("config.yaml")
    return detector, redactor


detector, redactor = get_engine()

# -----------------------------------------------------------------------------
# File Uploader & Options
# -----------------------------------------------------------------------------
uploaded_file = st.file_uploader(
    "Choose a Microsoft Word document (.docx)",
    type=["docx"],
    help="Supports documents up to 50 MB",
)

enable_ocr = st.checkbox(
    "Scan embedded images with OCR (Scanned PAN & Aadhaar ID cards)",
    value=True,
    help="Uses EasyOCR to detect and visually redact PII on scanned ID cards.",
)

if uploaded_file is not None:
    file_details = {
        "Filename": uploaded_file.name,
        "File size": f"{uploaded_file.size / 1024:.1f} KB",
    }
    st.caption(f"Selected: **{file_details['Filename']}** ({file_details['File size']})")

    if st.button("🚀 Redact Document", type="primary", use_container_width=True):
        progress_placeholder = st.empty()
        progress_placeholder.info("⏳ Processing document paragraphs and tables...")

        start_time = time.time()

        with tempfile.TemporaryDirectory(prefix="st_pii_") as temp_dir:
            in_path = Path(temp_dir) / uploaded_file.name
            out_path = Path(temp_dir) / f"Redacted_{uploaded_file.name}"

            with open(in_path, "wb") as f_in:
                f_in.write(uploaded_file.getbuffer())

            # 1. Native text redaction across paragraphs and tables
            total_redactions, audits = redactor.redact_document(
                docx_path=in_path,
                output_path=out_path,
                detector=detector,
                config_path="config.yaml",
            )

            # 2. Optional OCR post-pass for embedded images
            ocr_count = 0
            if enable_ocr:
                progress_placeholder.info("⏳ Scanning embedded images with EasyOCR...")
                try:
                    gc.collect()
                    from ocr_processor import process_embedded_images
                    ocr_doc = docx.Document(str(out_path))
                    ocr_count = process_embedded_images(ocr_doc, detector)
                    if ocr_count > 0:
                        ocr_doc.save(str(out_path))
                        total_redactions += ocr_count
                    gc.collect()
                except Exception as exc:
                    st.warning(f"OCR image pass skipped: {exc}")

            elapsed = time.time() - start_time
            progress_placeholder.empty()

            # Read redacted file bytes
            with open(out_path, "rb") as f_out:
                redacted_bytes = f_out.read()

            st.success(
                f"✅ Document processed successfully in **{elapsed:.2f} seconds**! "
                f"Applied **{total_redactions}** total redactions."
            )

            # Display category breakdown
            counts = Counter(audit.category for audit in audits)
            if ocr_count > 0:
                counts["IMAGE_OCR_REGIONS"] = ocr_count

            st.subheader("📊 Detected PII Breakdown")
            col1, col2 = st.columns(2)
            items = list(counts.items())
            mid = (len(items) + 1) // 2

            with col1:
                for cat, count in items[:mid]:
                    st.markdown(f"- **{cat}**: {count}")
            with col2:
                for cat, count in items[mid:]:
                    st.markdown(f"- **{cat}**: {count}")

            st.download_button(
                label=f"⬇️ Download Redacted_{uploaded_file.name}",
                data=redacted_bytes,
                file_name=f"Redacted_{uploaded_file.name}",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary",
                use_container_width=True,
            )

# -----------------------------------------------------------------------------
# Footer
# -----------------------------------------------------------------------------
st.markdown("---")
st.markdown(
    """
    <div style="text-align: center; color: #94a3b8; font-size: 0.85rem;">
        KSH International Limited Red Herring Prospectus PII Redaction & Anonymization Engine<br>
        9 Required PII Categories &bull; Consistent Faker Replacements &bull; EasyOCR Visual Blackout Masking
    </div>
    """,
    unsafe_allow_html=True,
)
