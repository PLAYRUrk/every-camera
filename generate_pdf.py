#!/usr/bin/env python3
"""Generate PDF documentation from README.md."""
import os
import re
import sys

try:
    from fpdf import FPDF
except ImportError:
    print("Install fpdf2: pip install fpdf2")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
README_PATH = os.path.join(SCRIPT_DIR, "README.md")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "README.pdf")

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
]


def find_font(name):
    for d in FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return path
    return None


class DocPDF(FPDF):
    """PDF generator with markdown inline formatting support."""

    def __init__(self, font_name, mono_name):
        super().__init__()
        self.fn = font_name
        self.mn = mono_name

    def write_markdown_line(self, text, size=10):
        """Write a line with inline **bold** and `code` formatting."""
        # Split into segments: (text, style) where style is "", "B", or "mono"
        parts = re.split(r'(\*\*.*?\*\*|`[^`]+`)', text)
        self.set_font(self.fn, "", size)
        line_h = size * 0.55
        for part in parts:
            if not part:
                continue
            if part.startswith("**") and part.endswith("**"):
                self.set_font(self.fn, "B", size)
                self.write(line_h, part[2:-2])
                self.set_font(self.fn, "", size)
            elif part.startswith("`") and part.endswith("`"):
                self.set_font(self.mn, "", size - 1)
                self.write(line_h, part[1:-1])
                self.set_font(self.fn, "", size)
            else:
                self.write(line_h, part)

    def write_bullet_markdown(self, text, size=10):
        """Write a bullet point with markdown formatting."""
        self.set_font(self.fn, "", size)
        self.cell(6, size * 0.55, "\u2022 ")
        self.write_markdown_line(text, size)
        self.ln(size * 0.55)


def generate_pdf():
    with open(README_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    regular = find_font("DejaVuSans.ttf")
    bold_f = find_font("DejaVuSans-Bold.ttf")
    mono = find_font("DejaVuSansMono.ttf")

    if regular and bold_f and mono:
        font_name = "DejaVu"
        mono_name = "DejaVuMono"
    else:
        font_name = "Helvetica"
        mono_name = "Courier"

    pdf = DocPDF(font_name, mono_name)
    pdf.set_auto_page_break(auto=True, margin=15)

    if regular and bold_f and mono:
        pdf.add_font("DejaVu", "", regular)
        pdf.add_font("DejaVu", "B", bold_f)
        pdf.add_font("DejaVuMono", "", mono)

    pdf.add_page()

    in_code_block = False
    in_table = False
    table_rows = []
    code_buf = []

    def write_code(text):
        pdf.set_font(mono_name, "", 8)
        pdf.set_fill_color(240, 240, 240)
        for line in text.split("\n"):
            pdf.cell(0, 4, "  " + line, new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.ln(2)

    def flush_table():
        nonlocal table_rows
        if not table_rows:
            return
        data = [r for r in table_rows if not all(c.strip().replace("-", "") == "" for c in r)]
        if not data:
            table_rows = []
            return
        n_cols = len(data[0])
        col_w = (pdf.w - 20) / n_cols
        for i, row in enumerate(data):
            style = "B" if i == 0 else ""
            pdf.set_font(font_name, style, 8)
            for cell in row:
                pdf.cell(col_w, 5, cell.strip(), border=1)
            pdf.ln()
        pdf.ln(2)
        table_rows = []

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        # Code block toggle
        if line.startswith("```"):
            if in_code_block:
                write_code("\n".join(code_buf))
                code_buf = []
                in_code_block = False
            else:
                if in_table:
                    flush_table()
                    in_table = False
                in_code_block = True
            continue

        if in_code_block:
            code_buf.append(line)
            continue

        # Table detection
        if "|" in line and line.strip().startswith("|"):
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r'^[-:]+$', c) for c in cols):
                in_table = True
                continue
            table_rows.append(cols)
            in_table = True
            continue

        if in_table:
            flush_table()
            in_table = False

        # Headers
        if line.startswith("# "):
            pdf.set_font(font_name, "B", 20)
            pdf.cell(0, 12, line[2:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)
        elif line.startswith("## "):
            pdf.ln(4)
            pdf.set_font(font_name, "B", 15)
            pdf.cell(0, 9, line[3:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif line.startswith("### "):
            pdf.ln(2)
            pdf.set_font(font_name, "B", 12)
            pdf.cell(0, 7, line[4:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif line.startswith("#### "):
            pdf.ln(1)
            pdf.set_font(font_name, "B", 11)
            pdf.cell(0, 6, line[5:].strip(), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
        elif line.startswith("- **"):
            # Bold list item with description
            m = re.match(r'^- \*\*(.+?)\*\*\s*(.*)$', line)
            if m:
                pdf.set_font(font_name, "B", 10)
                pdf.cell(6, 5, "\u2022 ")
                pdf.write(5, m.group(1))
                rest = m.group(2).strip()
                if rest:
                    # Strip leading dash/emdash
                    rest = re.sub(r'^[\u2014\-]+\s*', ' \u2014 ', rest)
                    pdf.set_font(font_name, "", 9)
                    pdf.write(5, rest)
                pdf.ln(5)
                pdf.ln(1)
            else:
                pdf.write_bullet_markdown(line[2:], 10)
                pdf.ln(1)
        elif line.startswith("- "):
            pdf.write_bullet_markdown(line[2:], 10)
            pdf.ln(1)
        elif line.strip() == "":
            pdf.ln(2)
        else:
            # Regular paragraph with inline formatting
            pdf.write_markdown_line(line, 10)
            pdf.ln(5)
            pdf.ln(1)

    if in_table:
        flush_table()

    pdf.output(OUTPUT_PATH)
    print(f"PDF generated: {OUTPUT_PATH}")


if __name__ == "__main__":
    generate_pdf()
