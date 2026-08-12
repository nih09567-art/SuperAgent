from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"E:\Program\SuperAgent\.tmp\defense-ppt\vendor")
import fitz  # type: ignore


source = Path(r"D:\xwechat_files\wxid_ddeayze1y1mm22_44d3\msg\file\2026-08\2025082916122218429(1).pdf")
output = Path(r"E:\Program\SuperAgent\.tmp\defense-ppt\pdf\pages")
output.mkdir(parents=True, exist_ok=True)

document = fitz.open(source)
for index, page in enumerate(document, start=1):
    pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
    pixmap.save(output / f"page-{index:02d}.png")

