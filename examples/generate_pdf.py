"""Create a synthetic selectable PDF table. Values are demo data only."""
import argparse
from pathlib import Path

import pymupdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("table-demo.pdf"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new path")
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=500, height=300)
        page.insert_text((40, 35), "Synthetic pressure table - not a factory standard", fontsize=12)
        xs, ys = [40, 150, 320, 460], [65, 105, 145, 185]
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        rows = [["Code", "Pressure (demo)", "Model"],
                ["E303", "0.60-0.80 MPa", "P-200"],
                ["E304", "0.50-0.70 MPa", "A-100"]]
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                page.insert_text((xs[c]+5, ys[r]+25), text, fontsize=11)
        pdf.save(args.output)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
