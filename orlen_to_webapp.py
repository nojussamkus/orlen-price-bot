import re
import io
import sys
import requests
import pdfplumber
from urllib.parse import urljoin
from datetime import datetime

# ========= NUSTATYMAI =========
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"

LIST_URLS = [
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",
]

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

# ========= PDF nuorodų surinkimas ir rikiavimas =========
def get_all_pdf_links(html: str, base_url: str):
    links = re.findall(r'href=["\']([^"\']+\.pdf)["\']', html, flags=re.I)
    links += re.findall(r'(https?://[^\s"\']+\.pdf)', html, flags=re.I)
    abs_links, seen = [], set()
    for l in links:
        u = urljoin(base_url, l)
        if u not in seen:
            seen.add(u)
            abs_links.append(u)
    return abs_links

def parse_date_from_url(url: str):
    for pat in (r"(\d{4})[-_](\d{2})[-_](\d{2})", r"(\d{4})(\d{2})(\d{2})"):
        m = re.search(pat, url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
    return None

def collect_candidate_pdfs():
    """Grąžina kandidatų sąrašą, surikiuotą nuo naujausio pagal datą URL’e (jei yra)."""
    all_links = []
    for url in LIST_URLS:
        html = http_get(url).text
        all_links.extend(get_all_pdf_links(html, url))
    if not all_links:
        raise RuntimeError("Neradau PDF nuorodų.")

    # rikiuojam: su data (desc) -> be datos (originalia tvarka)
    dated, nodate = [], []
    for u in all_links:
        d = parse_date_from_url(u)
        if d: dated.append((d, u))
        else: nodate.append(u)
    dated.sort(key=lambda x: x[0], reverse=True)
    ordered = [u for _, u in dated] + nodate

    # pašalinam negyvus (jei tokių yra) – ir paliekam tvarką
    alive = []
    for u in ordered:
        ok = False
        try:
            h = SESSION.head(u, timeout=15, allow_redirects=True)
            ok = 200 <= h.status_code < 400
        except Exception:
            pass
        if not ok:
            try:
                g = SESSION.get(u, timeout=15, stream=True)
                ok = 200 <= g.status_code < 400
                g.close()
            except Exception:
                ok = False
        if ok and u not in alive:
            alive.append(u)
    if not alive:
        raise RuntimeError("Rastos PDF nuorodos nepasiekiamos (HTTP).")
    return alive

# ========= PDF skaitymas ir parinkimas =========
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
    raw_third = nums[2]
    value = round(clean_number(raw_third), 2)
    return effective, value

# ========= Siuntimas į Apps Script =========
def post_to_webapp(date_str: str, price: float):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ========= MAIN =========
def main():
    try:
        candidates = collect_candidate_pdfs()
        print(f"[INFO] Rasta PDF nuorodų: {len(candidates)}")
        last_err = None

        for idx, pdf_url in enumerate(candidates, start=1):
            try:
                print(f"[INFO] ({idx}/{len(candidates)}) Bandau: {pdf_url}")
                pdf_bytes = http_get(pdf_url).content
                text = extract_pdf_text(pdf_bytes)
                date_str, price = pick_value_for_terminal(text)
                if not date_str:
                    date_str = "1970-01-01 00:00"
                print(f"[INFO] TINKA: date={date_str}, price={price}")
                resp = post_to_webapp(date_str, price)
                print("[INFO] WebApp atsakymas:", resp)
                return
            except Exception as e:
                last_err = e
                print(f"[WARN] Netinka ({type(e).__name__}): {e}")

        # jei nė vieno nepavyko apdoroti
        raise RuntimeError(f"Nepavyko rasti tinkamo PDF. Paskutinė klaida: {last_err}")

    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
