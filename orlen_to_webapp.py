import re, io, requests, pdfplumber

WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"

LIST_URLS = [
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
]

PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TERMINAL_LINE = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

def http_get(url, **kw):
    r = requests.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r

def find_latest_pdf_url():
    for url in LIST_URLS:
        html = http_get(url).text
        m = re.search(r'href="([^"]+\.pdf)".{0,200}Parsisi\u0173sti', html, re.I | re.S)
        if m:
            return requests.compat.urljoin(url, m.group(1))
        m2 = re.search(r'href="([^"]+\.pdf)"', html, re.I)
        if m2:
            return requests.compat.urljoin(url, m2.group(1))
    raise RuntimeError("Neradau PDF nuorodos.")

def extract_pdf_text(pdf_bytes):
    chunks = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            t = p.extract_text() or ""
            if t:
                chunks.append(t)
    return "\n".join(chunks)

def clean_number(s: str) -> float:
    s = s.replace("\xa0", " ").replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

def pick_value_for_terminal(pdf_text: str):
    mdate = DATE_RE.search(pdf_text)
    effective = None
    if mdate:
        effective = f"{mdate.group(1)} {mdate.group(2)}"
    lines = pdf_text.splitlines()
    prod_idxs = [i for i, l in enumerate(lines) if l.strip().startswith(PRODUCT_PREFIX)]
    if not prod_idxs:
        raise RuntimeError("Neradau produkto eilutės: " + PRODUCT_PREFIX)
    term_idxs = [i for i, l in enumerate(lines) if TERMINAL_LINE in l]
    if term_idxs:
        t = term_idxs[0]
        above = [i for i in prod_idxs if i < t]
        tgt_idx = above[-1] if above else prod_idxs[-1]
    else:
        tgt_idx = prod_idxs[-]()
