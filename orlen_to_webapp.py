import re
import io
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
from bs4 import BeautifulSoup

# ========= KONFIGŪRACIJA =========
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"
BASE_URL = "https://www.orlenlietuva.lt"
PAGE_URL = "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx"

PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TERMINAL_LINE = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
})

# ========= PAGALBINĖS =========
def http_get(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r

def clean_number(s: str) -> float:
    s = s.replace("\xa0", " ").replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

# ========= 1. PDF NUORODOS RADIMAS =========
def find_latest_pdf_url():
    html = http_get(PAGE_URL).text
    soup = BeautifulSoup(html, "html.parser")

    # Surandame VISAS nuorodas į Prices katalogą
    pdf_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/LT/Wholesale/Prices/" in href and href.lower().endswith(".pdf"):
            abs_url = urljoin(BASE_URL, href)
            pdf_links.append(abs_url)

    if not pdf_links:
        raise RuntimeError("Nerasta jokių PDF nuorodų su /LT/Wholesale/Prices/")

    # Rūšiuojame pagal datą pavadinime (jei yra)
    def extract_date(u):
        m = re.search(r"(\d{4})\D(\d{2})\D(\d{2})", u)
        if not m:
            return datetime.min
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return datetime.min

    pdf_links.sort(key=extract_date, reverse=True)
    latest = pdf_links[0]
    print(f"[INFO] Rasta {len(pdf_links)} PDF nuorodų. Naujausias: {latest}")
    return latest

# ========= 2. PDF SKAITYMAS =========
def extract_pdf_text(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = [p.extract_text() or "" for p in pdf.pages]
    joined = "\n".join(texts).strip()
    if not joined:
        raise RuntimeError("PDF tuščias arba ne tekstinis.")
    return joined

# ========= 3. DUOMENŲ IŠTRAUKIMAS =========
def pick_value_for_terminal(pdf_text: str):
    mdate = DATE_RE.search(pdf_text)
    date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()
    prod_idxs = [i for i, l in enumerate(lines) if l.strip().startswith(PRODUCT_PREFIX)]
    if not prod_idxs:
        raise RuntimeError("Neradau produkto eilutės.")
    term_idxs = [i for i, l in enumerate(lines) if TERMINAL_LINE in l]

    if term_idxs:
        t = term_idxs[0]
        above = [i for i in prod_idxs if i < t]
        idx = above[-1] if above else prod_idxs[-1]
    else:
        idx = prod_idxs[-1]

    row = lines[idx]
    nums = re.findall(r"[0-9][0-9\s.,]*", row)
    if len(nums) < 3:
        raise RuntimeError("Per mažai skaičių eilutėje.")
    val = round(clean_number(nums[2]), 2)
    return date_str, val

# ========= 4. ĮRAŠYMAS Į SHEETS =========
def post_to_webapp(date_str: str, price: float):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ========= MAIN =========
def main():
    try:
        pdf_url = find_latest_pdf_url()
        pdf_bytes = http_get(pdf_url).content
        pdf_text = extract_pdf_text(pdf_bytes)
        date_str, price = pick_value_for_terminal(pdf_text)
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
