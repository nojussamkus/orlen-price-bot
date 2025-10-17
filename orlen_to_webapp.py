import re
import io
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
from bs4 import BeautifulSoup

# ======= NUSTATYMAI =======
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"
PROTO_PAGE = "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx"

PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TERMINAL_LINE = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

def http_get(url, **kw):
    r = SESSION.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r

def clean_number(s: str) -> float:
    s = s.replace("\xa0", " ").replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

# ======= 1) Tvirtas PDF radimas iš „Kainų protokolai“ =======
def find_latest_protocol_pdf_url():
    html = http_get(PROTO_PAGE).text
    soup = BeautifulSoup(html, "html.parser")

    # 1) Surandame lentelę, kurios tekste yra „kain“ ir „protokol“
    table = None
    for tbl in soup.find_all("table"):
        text = tbl.get_text(" ", strip=True).lower()
        if "kain" in text and "protokol" in text:
            table = tbl
            break

    # 2) Iš lentelės paimam PIRMĄ <a href="...pdf"> (viršutinė eilutė = naujausia)
    if table:
        for tr in table.find_all("tr"):
            a = tr.find("a", href=re.compile(r"\.pdf($|\?)", re.I))
            if a and a.get("href"):
                return urljoin(PROTO_PAGE, a["href"])

    # 3) Fallback: paimam PIRMĄ .pdf nuorodą bet kur puslapyje
    a = soup.find("a", href=re.compile(r"\.pdf($|\?)", re.I))
    if a and a.get("href"):
        return urljoin(PROTO_PAGE, a["href"])

    # 4) Fallback: paimam PIRMĄ „Parsisi“ tekstinę nuorodą (nesvarbu struktūra)
    a = soup.find("a", string=re.compile("Parsisi", re.I))
    if a and a.get("href"):
        return urljoin(PROTO_PAGE, a["href"])

    raise RuntimeError("Neradau protokolų lentelės ar PDF nuorodos.")

# ======= 2) PDF tekstas =======
def extract_pdf_text(pdf_bytes: bytes) -> str:
    chunks = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            t = p.extract_text() or ""
            if t:
                chunks.append(t)
    txt = "\n".join(chunks).strip()
    if not txt:
        raise RuntimeError("PDF teksto nerasta (tuščias).")
    return txt

# ======= 3) Kainos paėmimas =======
def pick_value_for_terminal(pdf_text: str):
    mdate = DATE_RE.search(pdf_text)
    effective = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

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
        tgt_idx = prod_idxs[-1]

    row = lines[tgt_idx]
    nums = re.findall(r"[0-9][0-9\s.,]*", row)
    if len(nums) < 3:
        raise RuntimeError("Eilutėje per mažai skaitinių stulpelių.")
    value = round(clean_number(nums[2]), 2)
    return effective, value

# ======= 4) Siųsti į Google Apps Script =======
def post_to_webapp(date_str: str, price: float):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ======= MAIN =======
def main():
    try:
        pdf_url = find_latest_protocol_pdf_url()
        print(f"[INFO] Naujausio protokolo PDF: {pdf_url}")
        pdf_bytes = http_get(pdf_url).content

        text = extract_pdf_text(pdf_bytes)
        date_str, price = pick_value_for_terminal(text)
        if not date_str:
            date_str = "1970-01-01 00:00"

        print(f"[INFO] Ištraukti duomenys: date={date_str}, price={price}")
        resp = post_to_webapp(date_str, price)
        print("[INFO] WebApp atsakymas:", resp)

    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
