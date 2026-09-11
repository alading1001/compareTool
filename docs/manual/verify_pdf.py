from pathlib import Path
import hashlib
import json
import pdfplumber
from pypdf import PdfReader
from reportlab.pdfbase.ttfonts import TTFont

root = Path(__file__).resolve().parents[2]
work = root / '.tmp' / 'manual'
work.mkdir(parents=True, exist_ok=True)
path = root / 'docs' / 'CompareTool_使用说明书.pdf'
assert path.read_bytes() == (root / 'dist' / path.name).read_bytes(), 'docs/dist manual mismatch'
reader = PdfReader(path)
assert len(reader.pages) == 12
assert len(reader.outline) == 12
links = [a.get_object() for a in reader.pages[0].get('/Annots', [])]
assert len(links) == 11
for n, link in enumerate(links, 1):
    assert link['/Subtype'] == '/Link'
    ref = link['/Dest'][0]
    assert reader._get_page_number_by_indirect(ref) == n

font = TTFont('CheckYaHei', 'C:/Windows/Fonts/msyh.ttc', subfontIndex=0)
text = '\n'.join(page.extract_text() or '' for page in reader.pages)
missing = sorted({c for c in text if not c.isspace() and ord(c) not in font.face.charToGlyph})
assert not missing, f'Missing glyphs: {missing}'
embedded = {}
for page in reader.pages:
    for fref in page['/Resources']['/Font'].values():
        f = fref.get_object()
        desc = f.get('/FontDescriptor')
        if desc:
            desc = desc.get_object()
            embedded[str(f['/BaseFont'])] = any(k in desc for k in ['/FontFile', '/FontFile2', '/FontFile3'])
assert embedded and all(embedded.values())
bound_issues = []
with pdfplumber.open(path) as pdf:
    for n, pg in enumerate(pdf.pages, 1):
        for c in pg.chars:
            if c['x0'] < 45 or c['x1'] > pg.width-45 or c['top'] < 18 or c['bottom'] > pg.height-14:
                bound_issues.append((n,c['text'],c['x0'],c['x1'],c['top'],c['bottom']))
assert not bound_issues, bound_issues[:15]
result = {
    'pages':12, 'bookmarks':12, 'toc_links':11, 'missing_glyphs':missing,
    'embedded_fonts':embedded, 'out_of_bounds_characters':len(bound_issues),
    'bytes':path.stat().st_size,
    'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
}
(work/'qa_final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
