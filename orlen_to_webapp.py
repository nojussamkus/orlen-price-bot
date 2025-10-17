import re
import io
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
from bs4 import BeautifulSoup

WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"

PAGES = [
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",
]

TERMINAL_HDR  = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'
PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TARGET_COL_TEXT = "Bazinė kaina su akcizo"  # atpažinsim stulpelio antraštę

DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

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

PDF_RE  = re.compile(r"(https?://[^\s\"']+\.pdf(?:\?[^\s\"']*)?)", re.I)
HREF_RE = re.compile(r"\.pdf($|\?)", re.I)

def collect_pdf_links():
    out = []
    for page in PAGES:
        resp = http_get(page)
        base = resp.url
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if HREF_RE.search(a["href"]):
                out.append(urljoin(base, a["href"]))
        out += PDF_RE.findall(html)
    uniq, seen = [], set()
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq

def choose_latest_pdf(links):
    if not links: raise RuntimeError("Nerasta jokių .pdf nuorodų.")
    links.sort(key=lambda u: parse_date_from_str(u), reverse=True)
    print(f"[INFO] Rasta PDF nuorodų: {len(links)}. Naujausias: {links[0]}")
    return links[0]

# ---------- TABLE MODE (prioritetinis) ----------
def try_pick_from_table_strict(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        mdate = DATE_RE.search(full_text)
        date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

        in_block = False
        target_col_idx = None  # bandysim aptikti pagal antraštę

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

                    if TERMINAL_HDR in row_txt:
                        in_block = True
                        # kai patekome į bloką, bandome tame pačiame/sek. eilutėje susirasti stulpelių antraštes
                        # ieškome „Bazinė kaina su akcizo“
                        # (gali būti anksčiau faile – todėl target_col_idx paliksim ir panaudosim kai atsiras)
                        if TARGET_COL_TEXT.lower() in row_txt.lower():
                            # rask tikslią cell poziciją
                            for i, cell in enumerate(row):
                                if cell and TARGET_COL_TEXT.lower() in str(cell).lower():
                                    target_col_idx = i
                        continue

                    if in_block and ((row_txt.startswith("UAB ") or row_txt.startswith("AB ")) and "terminalas" in row_txt):
                        in_block = False

                    if not in_block:
                        # kol ne bloke – pasibandyti pagauti antraštes
                        if TARGET_COL_TEXT.lower() in row_txt.lower():
                            for i, cell in enumerate(row):
                                if cell and TARGET_COL_TEXT.lower() in str(cell).lower():
                                    target_col_idx = i
                        continue

                    # čia jau esame bloke
                    first = (row[0] or "").strip()
                    if not first.startswith(PRODUCT_PREFIX):
                        continue

                    # jei radome stulpelio indeksą – imame BŪTENT tą langelį
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

                    # jei nepavyko – imame 3-čią skaitinį elementą nuo eilutės pradžios (po pavadinimo)
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

        raise RuntimeError("Neradau reikiamos eilutės lentelėje.")

# ---------- TEXT MODE (atsarginis, tik tame bloke) ----------
def extract_pdf_text(pdf_bytes: bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texts = [p.extract_text() or "" for p in pdf.pages]
    txt = "\n".join(texts).strip()
    if not txt: raise RuntimeError("PDF tuščias arba ne tekstinis.")
    return txt

def smart_find_numbers(segment: str):
    segment = re.sub(r"(\d\.\d{2})(?=\d)", r"\1 ", segment)  # 556.31519.60 -> 556.31 519.60
    matches = re.findall(r"\d+(?:[.,]\d{1,3})?", segment)
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
    mdate = DATE_RE.search(pdf_text)
    date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()
    start = None
    for i, l in enumerate(lines):
        if TERMINAL_HDR in l:
            start = i + 1
            break
    if start is None:
        raise RuntimeError("Tekste neradau terminalo antraštės.")

    end = len(lines)
    for j in range(start, len(lines)):
        t = lines[j].strip()
        if (t.startswith("UAB ") or t.startswith("AB ")) and "terminalas" in t:
            end = j
            break

    block = lines[start:end]

    idx = None
    for k, l in enumerate(block):
        if l.strip().startswith(PRODUCT_PREFIX):
            idx = k
            break
    if idx is None:
        raise RuntimeError("Bloke neradau produkto eilutės.")

    # tik produkto eilutė + max 2 sekančios – kad nepatektų kito stulpelio ar eilutės skaičiai
    window = " ".join(block[idx: idx + 3])
    nums = smart_find_numbers(window)
    if len(nums) < 3:
        raise RuntimeError(f"Per mažai skaičių produkto eilutėje: {nums}")
    return date_str, round(nums[2], 2)

def post_to_webapp(date_str: str, price: float):
    r = SESSION.post(WEBHOOK_URL, json={"date": date_str, "price": price}, timeout=30)
    r.raise_for_status()
    return r.json()

def main():
    try:
        links = collect_pdf_links()
        pdf_url = choose_latest_pdf(links)
        pdf_bytes = http_get(pdf_url).content

        try:
            date_str, price = try_pick_from_table_strict(pdf_bytes)
            method = "table"
        except Exception as e_tbl:
            print(f"[WARN] Lentelės režimas nepavyko: {e_tbl} — perjungiu į tekstą.")
            text = extract_pdf_text(pdf_bytes)
            date_str, price = pick_from_text_in_block(text)
            method = "text"

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
