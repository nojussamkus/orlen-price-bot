import re
import io
import sys
import requests
import pdfplumber
from urllib.parse import urljoin
from datetime import datetime

# ======= NUSTATYMAI =======
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"

# Puslapiai, kuriuose ieškosim PDF nuorodų
LIST_URLS = [
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
]

# Produkto ir terminalo inkarai
PRODUCT_PREFIX = "Automobilinis 95 markės benzinas E10"
TERMINAL_LINE = 'Akcinės bendrovės "Orlen Lietuva" terminalas Juodeikių km, Mažeikių raj.'

# Antraštės data PDF viduje, pvz.: "Kainos galioja nuo 2025-10-15 09:00"
DATE_RE = re.compile(r"Kainos galioja nuo\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

# ======= HTTP =======
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

def http_get(url, **kw):
    r = SESSION.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r

# ======= PDF NUORODOS PAIEŠKA =======

def get_all_pdf_links(html: str, base_url: str):
    """Grąžina absoliučius PDF URL iš HTML teksto."""
    # href="...pdf" ir '...pdf'
    links = re.findall(r'href=["\']([^"\']+\.pdf)["\']', html, flags=re.I)
    # dar vienas „plačios“ paieškos variantas
    links += re.findall(r'(https?://[^\s"\']+\.pdf)', html, flags=re.I)
    # normalizuojam į absoliučius
    abs_links = []
    for link in links:
        abs_links.append(urljoin(base_url, link))
    # pašalinam dublikatus išlaikant eiliškumą
    seen = set()
    out = []
    for u in abs_links:
        if u not in seen:
            out.append(u)
            seen.add(u)
    return out

def parse_date_from_url(url: str):
    """
    Bando išsitraukti datą iš failo URL/pavadinimo, pvz. 2025-10-15 ar 2025_10_15 ar 20251015.
    Grąžina datetime arba None.
    """
    patterns = [
        r"(\d{4})[-_](\d{2})[-_](\d{2})",
        r"(\d{4})(\d{2})(\d{2})"
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            try:
                y, mth, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return datetime(y, mth, d)
            except Exception:
                pass
    return None

def choose_latest_link(links):
    """
    Jei bent ant kai kurių nuorodų yra data – rūšiuojam pagal ją (naujausia pirmoji).
    Jei ne – grąžinam pirmą rastą.
    Taip pat trumpai patikrinam, kad URL atsako 200 (HEAD/GET).
    """
    if not links:
        return None

    with_dates = []
    without_dates = []
    for u in links:
        d = parse_date_from_url(u)
        if d:
            with_dates.append((d, u))
        else:
            without_dates.append(u)

    # prioritetas – su data
    ordered = []
    if with_dates:
        with_dates.sort(key=lambda x: x[0], reverse=True)
        ordered = [u for _, u in with_dates] + without_dates
    else:
        ordered = links

    # grąžinam pirmą egzistuojantį (status 200)
    for u in ordered:
        try:
            # kartais HEAD blokuojamas – tada pabandysim GET su stream
            h = SESSION.head(u, timeout=15, allow_redirects=True)
            if 200 <= h.status_code < 400:
                return u
        except Exception:
            pass
        try:
            g = SESSION.get(u, timeout=15, stream=True)
            if 200 <= g.status_code < 400:
                g.close()
                return u
        except Exception:
            pass
    # jei niekas neatsako – grąžinam pirmą
    return ordered[0]

def find_latest_pdf_url():
    for url in LIST_URLS:
        html = http_get(url).text
        pdfs = get_all_pdf_links(html, url)
        if pdfs:
            chosen = choose_latest_link(pdfs)
            if chosen:
                return chosen
    raise RuntimeError("Neradau PDF nuorodos.")

# ======= PDF NUSKAITYMAS =======

def extract_pdf_text(pdf_bytes: bytes):
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
    """
    Grąžina (date_str 'YYYY-MM-DD HH:MM' arba None, price float)
    – paima 3-čią skaičių produkto eilutėje: 'Bazinė kaina su akcizo mokesčiu'.
    """
    # data iš antraštės
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
        tgt_idx = prod_idxs[-1]

    row = lines[tgt_idx]
    nums = re.findall(r"[0-9][0-9\s.,]*", row)
    if len(nums) < 3:
        raise RuntimeError("Eilutėje per mažai skaitinių stulpelių.")
    raw_third = nums[2]
    value = round(clean_number(raw_third), 2)
    return effective, value

# ======= SIUNTIMAS Į APPS SCRIPT =======

def post_to_webapp(date_str: str, price: float):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ======= MAIN =======

def main():
    pdf_url = find_latest_pdf_url()
    print(f"[INFO] Pasirinktas PDF: {pdf_url}")
    pdf_bytes = http_get(pdf_url).content

    text = extract_pdf_text(pdf_bytes)
    date_str, price = pick_value_for_terminal(text)
    if not date_str:
        # jei neradom datos PDF viršuje – vis tiek siųsim (Apps Script nukirps iki YYYY-MM-DD)
        date_str = "1970-01-01 00:00"

    print(f"[INFO] Ištraukti duomenys: date={date_str}, price={price}")
    resp = post_to_webapp(date_str, price)
    print("[INFO] WebApp atsakymas:", resp)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)
