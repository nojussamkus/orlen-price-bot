import re
import io
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
from bs4 import BeautifulSoup

# ================== KONFIGAS ==================
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"

PAGES = [
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",
]

# Tikslinis skyrius + eilutė
TERMINAL_HDR  = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"

# Data PDF’e
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# ================== HELPERS ==================
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

# ================== PDF NUORODOS ==================
PDF_RE  = re.compile(r"(https?://[^\s\"']+\.pdf(?:\?[^\s\"']*)?)", re.I)
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

# ================== LENTELĖS REŽIMAS ==================
def try_pick_from_table_strict(pdf_bytes: bytes):
    """
    Skaito pdfplumber lenteles, bet ima TIK tą bloką, kuris yra po
    TERMINAL_HDR antrašte ir iki kitos antraštės. Iš ten parenka
    eilutę, prasidedančią PRODUCT_PREFIX, ir grąžina 3-čią skaitinį stulpelį.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # data
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        mdate = DATE_RE.search(full_text)
        date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

        in_target_block = False

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

            # jei lentelės nesiseka, pereisim prie teksto režimo (bus fallback)
            for tbl in tables or []:
                for row in tbl:
                    if not row:
                        continue
                    row_txt = " ".join([c for c in row if c]).strip()

                    # įeinam į reikiamą bloką
                    if TERMINAL_HDR in row_txt:
                        in_target_block = True
                        continue
                    # išeinam, kai prasideda kitas blokas (paprastai prasideda nuo UAB/AB ir pan.)
                    if in_target_block and (row_txt.startswith("UAB ") or row_txt.startswith("AB ") or " terminalas " in row_txt and TERMINAL_HDR not in row_txt):
                        in_target_block = False

                    if not in_target_block:
                        continue

                    first_cell = (row[0] or "").strip()
                    if not first_cell.startswith(PRODUCT_PREFIX):
                        continue

                    # surenkam skaičius iš likusių stulpelių
                    nums = []
                    for cell in row[1:]:
                        if cell is None:
                            continue
                        for piece in re.findall(r"[0-9][0-9\s.,]*", str(cell)):
                            try:
                                nums.append(clean_number(piece))
                            except Exception:
                                pass

                    # tikimasi: pardavimo, akcizas, bazė su akcizu, PVM, su PVM
                    if len(nums) >= 3:
                        return date_str, round(nums[2], 2)

        raise RuntimeError("Lentelių režimu neradau reikiamos eilutės bloko.")

# ================== TEKSTO REŽIMAS (tik skyriuje) ==================
def extract_pdf_text(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = [p.extract_text() or "" for p in pdf.pages]
    txt = "\n".join(texts).strip()
    if not txt:
        raise RuntimeError("PDF tuščias arba ne tekstinis.")
    return txt

def smart_find_numbers(segment: str):
    # įterpiam tarpą po kiekvieno x.yy, jei po jo eina skaitmuo (sujungimų taisymas)
    fixed = re.sub(r"(\d\.\d{2})(?=\d)", r"\1 ", segment)
    matches = re.findall(r"\d+(?:[.,]\d{1,3})?", fixed)
    out = []
    for m in matches:
        try:
            v = clean_number(m)
            if 0 < v < 10000:
                out.append(v)
        except Exception:
            pass
    return out

def pick_from_text_in_block(pdf_text: str):
    """
    Iškanda tik tą teksto bloką, kuris yra po TERMINAL_HDR iki kito skyriaus,
    ir ten ieško mūsų produkto eilutės.
    """
    # data
    mdate = DATE_RE.search(pdf_text)
    date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()
    # surandam bloko ribas
    start = None
    for i, l in enumerate(lines):
        if TERMINAL_HDR in l:
            start = i + 1
            break
    if start is None:
        raise RuntimeError("Teksto režime neradau TERMINAL_HDR.")

    # baigiam ties pirma naujo skyriaus indikacija
    end = len(lines)
    for j in range(start, len(lines)):
        t = lines[j].strip()
        if (t.startswith("UAB ") or t.startswith("AB ")) and "terminalas" in t:
            end = j
            break

    block = lines[start:end]

    # surandam produkto eilutę
    idx = None
    for k, l in enumerate(block):
        if l.strip().startswith(PRODUCT_PREFIX):
            idx = k
            break
    if idx is None:
        raise RuntimeError("Bloke neradau produkto eilutės.")

    window = " ".join(block[idx: idx + 4])
    nums = smart_find_numbers(window)
    if len(nums) < 3:
        raise RuntimeError(f"Bloke per mažai skaičių: {nums}")
    return date_str, round(nums[2], 2)

# ================== POST į Sheets ==================
def post_to_webapp(date_str: str, price: float):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ================== MAIN ==================
def main():
    try:
        links = collect_pdf_links()
        pdf_url = choose_latest_pdf(links)
        pdf_bytes = http_get(pdf_url).content

        # 1) bandome lentelių režimu tiksliniame bloke
        try:
            date_str, price = try_pick_from_table_strict(pdf_bytes)
            method = "table-block"
        except Exception as e_tbl:
            print(f"[WARN] Lentelės blokas nepavyko: {e_tbl} — perjungiu į teksto bloką.")
            txt = extract_pdf_text(pdf_bytes)
            date_str, price = pick_from_text_in_block(txt)
            method = "text-block"

        if not date_str:
            date_str = "1970-01-01 00:00"

        print(f"[INFO] ({method}) {PRODUCT_PREFIX} @ Juodeikių/ Mažeikių: {price}")
        resp = post_to_webapp(date_str, price)
        print("[INFO] WebApp atsakymas:", resp)

    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
