import re
import io
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
from bs4 import BeautifulSoup

# ========== KONFIGAS ==========
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"

PAGES = [
    # tavo nurodytas puslapis – skenuojam pirma
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
    # atsarginis – „Kainų protokolai“
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",
]

PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TERMINAL_LINE  = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# ========== HELPERS ==========
def http_get(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r

def clean_number(s: str) -> float:
    s = s.replace("\xa0", " ").replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

def parse_date_from_str(s: str) -> datetime:
    for pat in (r"(\d{4})\D(\d{2})\D(\d{2})", r"(\d{4})(\d{2})(\d{2})"):
        m = re.search(pat, s)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
    return datetime.min

# ========== 1) Surenkam PDF nuorodas iš puslapių ==========
PDF_HREF_RE = re.compile(r'\.pdf($|\?)', re.I)
RAW_PDF_RE  = re.compile(r'(https?://[^\s"\']+?\.pdf(?:\?[^\s"\']*)?)', re.I)

def collect_pdf_links():
    found = []
    for page in PAGES:
        resp = http_get(page)
        base = resp.url  # realus URL po redirect
        html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        # <a href="...pdf">
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if PDF_HREF_RE.search(href):
                found.append(urljoin(base, href))

        # fallback: traukiam per raw regex (jei href'ai dinamiškai sudedami)
        for m in RAW_PDF_RE.findall(html):
            found.append(urljoin(base, m))

    # unikalizuojam
    uniq, seen = [], set()
    for u in found:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

def choose_latest_pdf(links):
    if not links:
        raise RuntimeError("Nerasta jokių .pdf nuorodų.")
    links.sort(key=lambda u: parse_date_from_str(u), reverse=True)
    latest = links[0]
    print(f"[INFO] Rasta PDF nuorodų: {len(links)}. Naujausias: {latest}")
    return latest

# ========== 2) PDF -> tekstas ==========
def extract_pdf_text(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = [p.extract_text() or "" for p in pdf.pages]
    text = "\n".join(texts).strip()
    if not text:
        raise RuntimeError("PDF tuščias arba ne tekstinis.")
    return text

# ========== 3) Ištraukiam datą + kainą ==========
def pick_value_for_terminal(pdf_text: str):
    mdate = DATE_RE.search(pdf_text)
    date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()
    prod_idxs = [i for i, l in enumerate(lines) if l.strip().startswith(PRODUCT_PREFIX)]
    if not prod_idxs:
        raise RuntimeError("Neradau produkto eilutės.")

    term_idxs = [i for i, l in enumerate(lines) if TERMINAL_LINE in l]
    if term_idxs:
        t0 = term_idxs[0]
        above = [i for i in prod_idxs if i < t0]
        idx = above[-1] if above else prod_idxs[-1]
    else:
        idx = prod_idxs[-1]

    # Imame skaičius iš kelias eiles (nes pdfplumber kartais „laužo“ stulpelius)
    window = lines[idx]
    nums = re.findall(r"[0-9][0-9\s.,]*", window)
    j = 1
    while len(nums) < 3 and idx + j < len(lines) and j <= 3:
        window += " " + lines[idx + j]
        nums = re.findall(r"[0-9][0-9\s.,]*", window)
        j += 1

    if len(nums) < 3:
        raise RuntimeError("Per mažai skaičių produkto eilutėje (net ir su +3 eilučių langu).")

    price = round(clean_number(nums[2]), 2)  # 3-ias = Bazinė kaina su akcizu
    return date_str, price

# ========== 4) POST į Apps Script ==========
def post_to_webapp(date_str: str, price: float):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ========== MAIN ==========
def main():
    try:
        links = collect_pdf_links()
        pdf_url = choose_latest_pdf(links)
        pdf_bytes = http_get(pdf_url).content

        text = extract_pdf_text(pdf_bytes)
        date_str, price = pick_value_for_terminal(text)
        if not date_str:
            date_str = "1970-01-01 00:00"

        print(f"[INFO] Data={date_str}, Kaina={price}")
        resp = post_to_webapp(date_str, price)
        print("[INFO] WebApp atsakymas:", resp)
    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
