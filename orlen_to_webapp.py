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

# ========= NAUDINGOS PAGALBINĖS =========
A_TAG_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)

def extract_links_with_text(html: str, base_url: str):
    """Grąžina [(abs_url, visible_text)] iš visų <a>…</a>."""
    out = []
    for href, text in A_TAG_RE.findall(html):
        url = urljoin(base_url, href.strip())
        # sutvarkom tekstą
        vis = re.sub(r"<[^>]+>", " ", text)
        vis = re.sub(r"\s+", " ", vis).strip()
        out.append((url, vis))
    return out

def parse_date_from_string(s: str):
    """Bando išsitraukti datą iš URL arba teksto: YYYY-MM-DD | YYYY_MM_DD | YYYYMMDD."""
    for pat in (r"(\d{4})[-_](\d{2})[-_](\d{2})", r"(\d{4})(\d{2})(\d{2})"):
        m = re.search(pat, s)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
    return None

def is_pdf_url(url: str):
    """Greita euristika – bet tikriname ir atsakymo Content-Type vėliau."""
    return url.lower().endswith(".pdf") or "pdf" in url.lower()

def head_or_get(url: str):
    """Grąžina (ok_bool, content_type_str)."""
    try:
        h = SESSION.head(url, timeout=15, allow_redirects=True)
        ct = h.headers.get("Content-Type", "")
        if 200 <= h.status_code < 400 and ct:
            return True, ct
    except Exception:
        pass
    try:
        g = SESSION.get(url, timeout=20, stream=True)
        ct = g.headers.get("Content-Type", "")
        ok = 200 <= g.status_code < 400
        g.close()
        return ok, ct
    except Exception:
        return False, ""

def collect_candidate_pdfs():
    """Surenkam VISAS kandidatų nuorodas su tekstais, dedam prioritetą 'protokol*', tikrinam Content-Type."""
    pairs = []
    for page in LIST_URLS:
        html = http_get(page).text
        pairs += extract_links_with_text(html, page)

    # pašalinam dublikatus išlaikant eiliškumą
    seen = set()
    uniq_pairs = []
    for u, t in pairs:
        if (u, t) not in seen:
            seen.add((u, t))
            uniq_pairs.append((u, t))

    # filtruojame tik tas, kurios atrodo kaip PDF arba galėtų grąžinti PDF
    maybe_pdf = [(u, t) for (u, t) in uniq_pairs if is_pdf_url(u) or "protokol" in t.lower() or "pdf" in t.lower()]

    # patikriname HEAD/GET ir pasiliekame tik tas, kurių Content-Type rodo pdf
    validated = []
    for u, t in maybe_pdf:
        ok, ct = head_or_get(u)
        if ok and "pdf" in ct.lower():
            validated.append((u, t))

    if not validated:
        raise RuntimeError("Neradau PDF kandidatų su PDF turiniu.")

    # scoring: 1) ar tekste yra 'protokol'/'protokolas', 2) data (desc)
    def score(item):
        u, t = item
        prot = 1 if ("protokol" in t.lower()) else 0
        d = parse_date_from_string(u) or parse_date_from_string(t) or datetime.min
        # didesnis geriau: (prot, data)
        return (prot, d)

    validated.sort(key=score, reverse=True)
    return [u for (u, _) in validated]

# ========= PDF PARSINIMAS =========
def extract_pdf_text(pdf_bytes: bytes):
    chunks = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            text = p.extract_text() or ""
            if text:
                chunks.append(text)
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

# ========= SIUNTIMAS Į APPS SCRIPT =========
def post_to_webapp(date_str: str, price: float):
    payload = {"date": date_str, "price": price}
    r = SESSION.post(WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# ========= MAIN =========
def main():
    try:
        candidates = collect_candidate_pdfs()
        print(f"[INFO] Kandidatų (PDF) rasta: {len(candidates)}")
        last_err = None

        for i, pdf_url in enumerate(candidates, 1):
            try:
                print(f"[INFO] ({i}/{len(candidates)}) Bandau: {pdf_url}")
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

        raise RuntimeError(f"Nepavyko rasti tinkamo PDF. Paskutinė klaida: {last_err}")

    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
