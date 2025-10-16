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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
})

def http_get(url, **kw):
    r = SESSION.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r

# ========= PDF NUORODŲ RADIMAS =========
def get_all_pdf_links(html, base_url):
    links = re.findall(r'href=["\']([^"\']+\.pdf)["\']', html, flags=re.I)
    links += re.findall(r'(https?://[^\s"\']+\.pdf)', html, flags=re.I)
    abs_links = [urljoin(base_url, l) for l in links]
    seen, out = set(), []
    for u in abs_links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def parse_date_from_url(url):
    pats = [r"(\d{4})[-_](\d{2})[-_](\d{2})", r"(\d{4})(\d{2})(\d{2})"]
    for pat in pats:
        m = re.search(pat, url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
    return None

def choose_latest_link(links):
    if not links:
        return None
    dated, nodate = [], []
    for u in links:
        d = parse_date_from_url(u)
        (dated if d else nodate).append((d, u) if d else u)
    ordered = [u for d, u in sorted(dated, key=lambda x: x[0], reverse=True)] + nodate if dated else links
    for u in ordered:
        try:
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
    return ordered[0]

def find_latest_pdf_url():
    all_links = []
    for url in LIST_URLS:
        html = http_get(url).text
        all_links.extend(get_all_pdf_links(html, url))
    if not all_links:
        raise RuntimeError("Neradau PDF nuorodų.")
    return choose_latest_link(all_links)

# ========= PDF TEKSTO NUSKAITYMAS =========
def extract_pdf_text(pdf_bytes):
    chunks = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            txt = p.extract_text() or ""
            if txt:
                chunks.append(txt)
    return "\n".join(chunks)

def clean_number(s):
    s = s.replace("\xa0", " ").replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

def pick_value_for_terminal(pdf_text):
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

# ========= API SIUNTIMAS =========
def post_to_webapp(date_str, price):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ========= MAIN =========
def main():
    try:
        pdf_url = find_latest_pdf_url()
        print(f"[INFO] Pasirinktas PDF: {pdf_url}")
        pdf_bytes = http_get(pdf_url).content
        text = extract_pdf_text(pdf_bytes)

        try:
            date_str, price = pick_value_for_terminal(text)
        except RuntimeError as e:
            print(f"[WARN] {e} — bandau kitą PDF (protokol)...")
            htmls = []
            for u in LIST_URLS:
                htmls.append(http_get(u).text)
            alt_links = []
            for h, base in zip(htmls, LIST_URLS):
                alt_links += [url for url in get_all_pdf_links(h, base) if "protokol" in url.lower()]
            if not alt_links:
                raise RuntimeError("Neradau alternatyvaus PDF su 'protokol'.")
            pdf_url = choose_latest_link(alt_links)
            print(f"[INFO] Bandau alternatyvų PDF: {pdf_url}")
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
