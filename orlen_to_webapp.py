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

# Puslapis su lentele "Kainų protokolai"
PROTO_PAGE = "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx"

# Produkto/terminalo inkarai
PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TERMINAL_LINE = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

# HTTP session
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
})


# ======= Pagalbinės =======

def http_get(url, **kw):
    r = SESSION.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r

def clean_number(s: str) -> float:
    s = s.replace("\xa0", " ").replace(" ", "")
    s = s.replace(",", ".")
    return float(s)


# ======= 1) Rasti naujausio protokolo PDF iš lentelės =======

def find_latest_protocol_pdf_url():
    """
    Parsina PROTO_PAGE lentelę "Kainų protokolai":
    - ima pirmą eilutę (naujausią datą),
    - paima dešinės skilties nuorodą "Parsisiųsti",
    - grąžina absoliutų PDF URL.
    Jei dėl kokios nors priežasties nepavyksta, bando rasti pirma .pdf nuorodą puslapyje.
    """
    html = http_get(PROTO_PAGE).text
    soup = BeautifulSoup(html, "html.parser")

    # Surandam lentelę su stulpeliais "Data" ir "Kainų protokolai"
    table = None
    for tbl in soup.find_all("table"):
        head_txt = " ".join((tbl.thead.get_text(" ", strip=True) if tbl.thead else tbl.get_text(" ", strip=True))).lower()
        if "data" in head_txt and "kainų protokolai" in head_txt:
            table = tbl
            break
    if not table:
        # fallback – imti pirmą "Parsisiųsti" nuorodą puslapyje
        a = soup.find("a", string=lambda s: s and "Parsisi" in s)
        if a and a.get("href"):
            return urljoin(PROTO_PAGE, a["href"])
        # dar vienas fallback – pirma .pdf nuoroda
        a = soup.find("a", href=lambda h: h and h.lower().endswith(".pdf"))
        if a:
            return urljoin(PROTO_PAGE, a["href"])
        raise RuntimeError("Neradau protokolų lentelės ar PDF nuorodos.")

    # Imame pirmą (viršutinę) eilutę <tr> su data ir "Parsisiųsti"
    first_row = None
    for tr in table.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if len(tds) >= 2 and "Parsisi" in tds[-1].get_text():
            first_row = tr
            break
    if not first_row:
        raise RuntimeError("Neradau pirmos protokolo eilutės su 'Parsisiųsti'.")

    link = first_row.find("a", href=True)
    if not link:
        raise RuntimeError("Neradau PDF nuorodos 'Parsisiųsti' langelyje.")
    return urljoin(PROTO_PAGE, link["href"])


# ======= 2) Ištraukti tekstą iš PDF =======

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


# ======= 3) Rasti datą ir kainą PDF’e =======

def pick_value_for_terminal(pdf_text: str):
    """
    Grąžina (date_str 'YYYY-MM-DD HH:MM' arba None, price float).
    Eina per teksto eilutes, randa produkto eilutę ir paima 3-ią skaičių –
    'Bazinė kaina su akcizo mokesčiu'.
    """
    mdate = DATE_RE.search(pdf_text)
    effective = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()
    prod_idxs = [i for i, l in enumerate(lines) if l.strip().startswith(PRODUCT_PREFIX)]
    if not prod_idxs:
        raise RuntimeError("Neradau produkto eilutės: " + PRODUCT_PREFIX)

    # rasti artimiausią produkto eilutę virš terminalo aprašymo (jei toks yra)
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
            # jei dėl kokių nors priežasčių nepavyko paimti datos – vis tiek siųsti
            date_str = "1970-01-01 00:00"

        print(f"[INFO] Ištraukti duomenys: date={date_str}, price={price}")
        resp = post_to_webapp(date_str, price)
        print("[INFO] WebApp atsakymas:", resp)

    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
