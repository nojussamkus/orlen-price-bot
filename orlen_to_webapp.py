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
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
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

# ========== 1) Surenkam PDF nuorodas ==========
PDF_RE = re.compile(r"(https?://[^\s\"']+\.pdf(?:\?[^\s\"']*)?)", re.I)
HREF_RE = re.compile(r"\.pdf($|\?)", re.I)

def collect_pdf_links():
    found = []
    for page in PAGES:
        resp = http_get(page)
        base = resp.url
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if HREF_RE.search(a["href"]):
                found.append(urljoin(base, a["href"]))
        found += PDF_RE.findall(html)
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

# ========== 2) Lentelės skaitymas (pirmas bandymas) ==========
def try_pick_from_table(pdf_bytes: bytes):
    """
    Bando ištraukti 3-ią skaitinį stulpelį (Bazinė kaina su akcizu) iš lentelės.
    Grąžina (date_str, price) arba pakelia klaidą, jei nepavyko.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # data iš bet kurio puslapio teksto
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        mdate = DATE_RE.search(full_text)
        date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

        # einam per puslapius, kol randam lentelę su mūsų eilute
        for p in pdf.pages:
            tables = p.extract_tables(
                table_settings={
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "intersection_tolerance": 5,
                    "snap_tolerance": 3,
                    "join_tolerance": 3,
                    "edge_min_length": 20,
                }
            )
            for tbl in tables or []:
                for row in tbl:
                    if not row:
                        continue
                    first = (row[0] or "").strip()
                    if first.startswith(PRODUCT_PREFIX):
                        # rinkti skaičius iš likusių stulpelių
                        nums = []
                        for cell in row[1:]:
                            if cell is None:
                                continue
                            # kai kuriuose pdf skaičiai būna su tarpais tūkstančiams
                            m = re.findall(r"[0-9][0-9\s.,]*", str(cell))
                            for piece in m:
                                try:
                                    nums.append(clean_number(piece))
                                except Exception:
                                    pass
                        # pagal stulpelius: [pardavimo, akcizas, bazė su akcizu, PVM, su PVM]
                        if len(nums) >= 3:
                            price = round(nums[2], 2)
                            return date_str, price
        raise RuntimeError("Lentelės režimu nepavyko rasti eilutės/stulpelio.")

# ========== 3) Teksto režimas (atsarginis) ==========
def extract_pdf_text(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = [p.extract_text() or "" for p in pdf.pages]
    txt = "\n".join(texts).strip()
    if not txt:
        raise RuntimeError("PDF tuščias arba ne tekstinis.")
    return txt

def smart_find_numbers(segment: str):
    fixed = re.sub(r"(\d\.\d{2})(?=\d)", r"\1 ", segment)  # 556.31519.60 -> 556.31 519.60
    matches = re.findall(r"\d+(?:[.,]\d{1,3})?", fixed)
    cleaned = []
    for m in matches:
        try:
            val = clean_number(m)
            if 0 < val < 10000:
                cleaned.append(val)
        except Exception:
            continue
    return cleaned

def pick_from_text(pdf_text: str):
    mdate = DATE_RE.search(pdf_text)
    date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()
    prod_idxs = [i for i, l in enumerate(lines) if l.strip().startswith(PRODUCT_PREFIX)]
    if not prod_idxs:
        raise RuntimeError("Neradau produkto eilutės (teksto režimas).")

    term_idxs = [i for i, l in enumerate(lines) if TERMINAL_LINE in l]
    if term_idxs:
        t0 = term_idxs[0]
        above = [i for i in prod_idxs if i < t0]
        idx = above[-1] if above else prod_idxs[-1]
    else:
        idx = prod_idxs[-1]

    window = " ".join(lines[idx: idx + 4])
    nums = smart_find_numbers(window)
    if len(nums) < 3:
        raise RuntimeError(f"Per mažai skaičių teksto režimu: {nums}")
    price = round(nums[2], 2)
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

        # 1) bandome per lentelę
        try:
            date_str, price = try_pick_from_table(pdf_bytes)
            method = "table"
        except Exception as e_table:
            print(f"[WARN] Lentelės skaitymas nepavyko: {e_table} — perjungiu į teksto režimą.")
            text = extract_pdf_text(pdf_bytes)
            date_str, price = pick_from_text(text)
            method = "text"

        if not date_str:
            date_str = "1970-01-01 00:00"

        print(f"[INFO] ({method}) Data={date_str}, Kaina={price}")
        resp = post_to_webapp(date_str, price)
        print("[INFO] WebApp atsakymas:", resp)
    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
