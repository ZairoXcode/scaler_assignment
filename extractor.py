"""DOCX extraction module for paragraphs, table cells, headers, and footers."""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List
import docx
from docx.text.paragraph import Paragraph


@dataclass
class ParagraphBlock:
    """Represents an extracted text block with its document location and underlying docx object."""
    text: str
    location: str
    raw_paragraph: Paragraph


class DocxExtractor:
    """Extracts text elements from Microsoft Word documents while preserving python-docx references."""

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"File not found: {self.file_path}")
        self.doc = docx.Document(str(self.file_path))

    def iter_blocks(self) -> Iterator[ParagraphBlock]:
        """Iterate over body paragraphs, table cells, headers, and footers in document order.

        Deduplicates paragraphs based on their underlying XML element identity id(p._p)
        to prevent duplicate extraction and subsequent double-redaction caused by merged
        table cells or repeated section references.
        """
        seen_paragraphs: set[int] = set()

        # 1. Body paragraphs
        for p_idx, p in enumerate(self.doc.paragraphs):
            p_id = id(p._p)
            if p_id in seen_paragraphs:
                continue
            seen_paragraphs.add(p_id)
            yield ParagraphBlock(
                text=p.text,
                location=f"Paragraph {p_idx}",
                raw_paragraph=p,
            )

        # 2. Table cells
        for t_idx, table in enumerate(self.doc.tables):
            for r_idx, row in enumerate(table.rows):
                for c_idx, cell in enumerate(row.cells):
                    for cp_idx, cp in enumerate(cell.paragraphs):
                        cp_id = id(cp._p)
                        if cp_id in seen_paragraphs:
                            continue
                        seen_paragraphs.add(cp_id)
                        yield ParagraphBlock(
                            text=cp.text,
                            location=f"Table {t_idx} [{r_idx}, {c_idx}] p{cp_idx}",
                            raw_paragraph=cp,
                        )

        # 3. Headers and footers across sections
        for s_idx, section in enumerate(self.doc.sections):
            if section.header is not None:
                for hp_idx, hp in enumerate(section.header.paragraphs):
                    if hp.text.strip():
                        hp_id = id(hp._p)
                        if hp_id in seen_paragraphs:
                            continue
                        seen_paragraphs.add(hp_id)
                        yield ParagraphBlock(
                            text=hp.text,
                            location=f"Section {s_idx} Header p{hp_idx}",
                            raw_paragraph=hp,
                        )

            if section.footer is not None:
                for fp_idx, fp in enumerate(section.footer.paragraphs):
                    if fp.text.strip():
                        fp_id = id(fp._p)
                        if fp_id in seen_paragraphs:
                            continue
                        seen_paragraphs.add(fp_id)
                        yield ParagraphBlock(
                            text=fp.text,
                            location=f"Section {s_idx} Footer p{fp_idx}",
                            raw_paragraph=fp,
                        )

    def extract_all(self) -> List[ParagraphBlock]:
        """Extract all blocks into a list."""
        return list(self.iter_blocks())
