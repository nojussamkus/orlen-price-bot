import re
import io
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
from bs4 import BeautifulSoup

# ===================== KONFIGAS =====================
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"

# Iš čia renkamės PDF nuorodas (pirmas – prioritetinis)
PAGES = [
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",
]

# Tikslus skyrius ir produkto eilutė
TERMINAL_HDR   = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TARGET_COL_TEXT = "Bazinė kaina su akcizo"  # stulpelio antraštė

# Data PDF'e
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

# HTTP sesija
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


# ===================== PAGALBINĖS =====================
def http_get(url: str) -> requests.Response:
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r

def clean_number(s: str) -> float:
    s = s.replace("\u00A0", " ")  # NBSP -> space
    s = s.replace(" ", "")
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


# ===================== PDF NUORODOS =====================
PDF_RE  = re.compile(r"(https?://[^\s\"']+\.pdf(?:\?[^\s\"']*)?)", re.I)
HREF_RE = re.compile(r"\.pdf($|\?)", re.I)

def collect_pdf_links():
    found = []
    for page in PAGES:
        resp = http_get(page)
        base = resp.url
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # <a href="...pdf">
        for a in soup.find_all("a", href=True):
            if HREF_RE.search(a["href"]):
                found.append(urljoin(base, a["href"]))

        # fallback: „pliki“ PDF URL'ai tekste
        found += PDF_RE.findall(html)

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


# ===================== LENTELĖS REŽIMAS =====================
def try_pick_from_table_strict(pdf_bytes: bytes):
    """
    Skaito pdfplumber lenteles. Eina tik per bloką po TERMINAL_HDR.
    Ten bando rasti stulpelio indeksą pagal TARGET_COL_TEXT.
    Jei neranda – ima 3-čią skaitinį langelį toje eilutėje.
    Grąžina (date_str, price) arba kelia klaidą.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # data
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        mdate = DATE_RE.search(full_text)
        date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

        in_block = False
        target_col_idx = None

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
                    row_txt = " ".join([c for c in row if c]).strip()

                    # įėjimas į bloką
                    if TERMINAL_HDR in row_txt:
                        in_block = True
                        # bandome pagauti antraščių eilutę tame pačiame ar sekančiame kontekste
                        if TARGET_COL_TEXT.lower() in row_txt.lower():
                            for i, cell in enumerate(row):
                                if cell and TARGET_COL_TEXT.lower() in str(cell).lower():
                                    target_col_idx = i
                        continue

                    # išėjimas iš bloko – kitas terminalo headingas
                    if in_block and ((row_txt.startswith("UAB ") or row_txt.startswith("AB ")) and "terminalas" in row_txt):
                        in_block = False

                    # antraščių „pagavimas“ jei jos ateina kiek anksčiau ar vėliau
                    if TARGET_COL_TEXT.lower() in row_txt.lower():
                        for i, cell in enumerate(row):
                            if cell and TARGET_COL_TEXT.lower() in str(cell).lower():
                                target_col_idx = i

                    if not in_block:
                        continue

                    # produkto eilutė
                    first = (row[0] or "").strip()
                    if not first.startswith(PRODUCT_PREFIX):
                        continue

                    # 1) jei žinome stulpelio indeksą – imame būtent jį
                    if target_col_idx is not None and target_col_idx < len(row):
                        cell = row[target_col_idx]
                        if cell:
                            nums = re.findall(r"[0-9][0-9\s.,]*", str(cell))
                            for piece in nums:
                                try:
                                    price = round(clean_number(piece), 2)
                                    return date_str, price
                                except Exception:
                                    pass

                    # 2) atsarginis – 3-ias skaitinis langelis eilutėje po pavadinimo
                    nums = []
                    for cell in row[1:]:
                        if cell is None:
                            continue
                        for piece in re.findall(r"[0-9][0-9\s.,]*", str(cell)):
                            try:
                                nums.append(clean_number(piece))
                            except Exception:
                                pass
                    if len(nums) >= 3:
                        return date_str, round(nums[2], 2)

        raise RuntimeError("Lentelių režimu neradau reikiamos eilutės bloko.")


# ===================== TEKSTO REŽIMAS =====================
def extract_pdf_text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = [p.extract_text() or "" for p in pdf.pages]
    txt = "\n".join(texts).strip()
    if not txt:
        raise RuntimeError("PDF tuščias arba ne tekstinis.")
    return txt

def smart_find_numbers(segment: str):
    """
    Grąžina TIK „piniginius“ skaičius su 2 skaitmenimis po kablelio (xx.xx ar xx,xx),
    tvarko sujungimus (pvz. '1054.20541.20' -> '1054.20 541.20'), leidžia tūkstančių grupes.
    """
    # įterpiam tarpą tarp dviejų piniginių skaičių, jei jie suklijuoti
    segment = re.sub(r"(\d[.,]\d{2})(?=\d)", r"\1 ", segment)

    money_pat = re.compile(
        r"(?<!\d)"                                 # ne prieš kitą skaitmenį
        r"(\d{1,3}(?:[ \u00A0]\d{3})*|\d+)"        # 1-3 sk. + tūkst. grupės arba vientisas
        r"[.,]\d{2}"                               # kablelis/taškas + 2 sk.
        r"(?!\d)",                                 # po to ne skaitmuo
    )

    vals = []
    for m in re.finditer(money_pat, segment):
        token = m.group(0).replace("\u00A0", " ").replace(" ", "").replace(",", ".")
        try:
            v = float(token)
            if 0 < v < 10000:
                vals.append(v)
        except Exception:
            pass
    return vals

def pick_from_text_in_block(pdf_text: str):
    """
    Teksto režimas: imame tik bloką po TERMINAL_HDR iki kito skyriaus.
    Toje atkarpoje randame produkto eilutę ir iš jos (plius max 2 sek. eilutės)
    paimame TREČIĄ piniginį skaičių.
    """
    # data
    mdate = DATE_RE.search(pdf_text)
    date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()

    # start – terminalo antraštė
    start = None
    for i, l in enumerate(lines):
        if TERMINAL_HDR in l:
            start = i + 1
            break
    if start is None:
        raise RuntimeError("Tekste neradau terminalo antraštės.")

    # end – kitas terminalo blokas
    end = len(lines)
    for j in range(start, len(lines)):
        t = lines[j].strip()
        if (t.startswith("UAB ") or t.startswith("AB ")) and "terminalas" in t:
            end = j
            break

    block = lines[start:end]

    # produkto eilutė
    idx = None
    for k, l in enumerate(block):
        if l.strip().startswith(PRODUCT_PREFIX):
            idx = k
            break
    if idx is None:
        raise RuntimeError("Bloke neradau produkto eilutės.")

    # tik produkto eilutė + iki 2 sek. eilučių (jei PDF „laužo“ stulpelius)
    window = " ".join(block[idx: idx + 3])
    nums = smart_find_numbers(window)
    if len(nums) < 3:
        raise RuntimeError(f"Per mažai piniginių skaičių produkto eilutėje: {nums}")

    # 3-ias piniginis skaičius – „Bazinė kaina su akcizo mokesčiu“
    return date_str, round(nums[2], 2)


# ===================== SIUNTIMAS Į SHEETS =====================
def post_to_webapp(date_str: str, price: float):
    r = SESSION.post(WEBHOOK_URL, json={"date": date_str, "price": price}, timeout=30)
    r.raise_for_status()
    return r.json()


# ===================== MAIN =====================
def main():
    try:
        links = collect_pdf_links()
        pdf_url = choose_latest_pdf(links)
        pdf_bytes = http_get(pdf_url).content

        try:
            date_str, price = try_pick_from_table_strict(pdf_bytes)
            method = "table-block"
        except Exception as e_tbl:
            print(f"[WARN] Lentelės režimas nepavyko: {e_tbl} — perjungiu į teksto bloką.")
            text = extract_pdf_text(pdf_bytes)
            date_str, price = pick_from_text_in_block(text)
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
