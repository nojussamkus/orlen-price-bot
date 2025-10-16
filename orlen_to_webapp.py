{\rtf1\ansi\ansicpg1252\cocoartf2822
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 import re, io, requests, pdfplumber\
\
# ---- NUSTATYMAI ----\
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbzkz6TJAGXtUDotTsXxYnPCmtBXNdI73Yq7g61TapYTAWIgujqgJ_S2XajI9FHMK_Y9rg/exec"\
\
# Puslapis su naujausio PDF nuoroda (ORLEN kain\uc0\u371  protokolai)\
LIST_URLS = [\
    # oficialus puslapis\
    "https://www.orlenlietuva.lt/LT/Wholesale/Puslapiai/Kainu-protokolai.aspx",\
    # vidinis s\uc0\u261 ra\'9aas (jei pirmasis negr\u261 \'9eint\u371  PDF)\
    "https://www.orlenlietuva.lt/lt/wholesale/_layouts/f2hPriceTable/default.aspx",\
]\
\
# Produkto ir terminalo \'84inkarai\'93\
PRODUCT_PREFIX = "Automobilinis 95 mark\uc0\u279 s benzinas E10"\
TERMINAL_LINE = 'Akcin\uc0\u279 s bendrov\u279 s "Orlen Lietuva" terminalas Juodeiki\u371  km, Ma\'9eeiki\u371  raj.'\
\
# PDF antra\'9at\uc0\u279 s data, pvz.: "Kainos galioja nuo 2025-10-15 09:00"\
DATE_RE = re.compile(r"Kainos galioja nuo\\s+(\\d\{4\}-\\d\{2\}-\\d\{2\})\\s+(\\d\{1,2\}:\\d\{2\})")\
\
def http_get(url, **kw):\
    r = requests.get(url, timeout=30, **kw)\
    r.raise_for_status()\
    return r\
\
def find_latest_pdf_url():\
    for url in LIST_URLS:\
        html = http_get(url).text\
        # 1) bandome rasti "Parsisi\uc0\u371 sti" nuorod\u261 \
        m = re.search(r'href="([^"]+\\.pdf)".\{0,200\}Parsisi\\u0173sti', html, re.I | re.S)\
        if m:\
            return requests.compat.urljoin(url, m.group(1))\
        # 2) fallback \'96 paimti pirm\uc0\u261  .pdf nuorod\u261  puslapyje\
        m2 = re.search(r'href="([^"]+\\.pdf)"', html, re.I)\
        if m2:\
            return requests.compat.urljoin(url, m2.group(1))\
    raise RuntimeError("Neradau PDF nuorodos.")\
\
def extract_pdf_text(pdf_bytes):\
    chunks = []\
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:\
        for p in pdf.pages:\
            t = p.extract_text() or ""\
            if t: chunks.append(t)\
    return "\\n".join(chunks)\
\
def clean_number(s: str) -> float:\
    s = s.replace("\\xa0", " ").replace(" ", "")\
    s = s.replace(",", ".")\
    return float(s)\
\
def pick_value_for_terminal(pdf_text: str):\
    """\
    Suranda dat\uc0\u261 , eilutes su produktu ir terminalu,\
    paima 3-i\uc0\u261  skai\u269 i\u371  produkto eilut\u279 je = 'Bazin\u279  kaina su akcizo mokes\u269 iu'\
    """\
    # data\
    mdate = DATE_RE.search(pdf_text)\
    effective = None\
    if mdate:\
        effective = f"\{mdate.group(1)\} \{mdate.group(2)\}"  # "YYYY-MM-DD HH:MM"\
    # eilut\uc0\u279 s\
    lines = pdf_text.splitlines()\
    prod_idxs = [i for i,l in enumerate(lines) if l.strip().startswith(PRODUCT_PREFIX)]\
    if not prod_idxs:\
        raise RuntimeError("Neradau produkto eilut\uc0\u279 s: " + PRODUCT_PREFIX)\
    term_idxs = [i for i,l in enumerate(lines) if TERMINAL_LINE in l]\
    # parenkam produkto eilut\uc0\u281 : ar\u269 iausi\u261  vir\'9a terminalo, jei pavyksta; kitaip paskutin\u281 \
    if term_idxs:\
        t = term_idxs[0]\
        above = [i for i in prod_idxs if i < t]\
        tgt_idx = above[-1] if above else prod_idxs[-1]\
    else:\
        tgt_idx = prod_idxs[-1]\
    row = lines[tgt_idx]\
    # visi skai\uc0\u269 iai eilut\u279 je\
    nums = re.findall(r"[0-9][0-9\\s.,]*", row)\
    if len(nums) < 3:\
        raise RuntimeError("Eilut\uc0\u279 je per ma\'9eai skaitini\u371  stulpeli\u371 .")\
    raw_third = nums[2]  # 3-ias = Bazin\uc0\u279  kaina su akcizo mokes\u269 iu\
    value = round(clean_number(raw_third), 2)\
    return effective, value\
\
def post_to_webapp(date_str: str, price: float):\
    payload = \{"date": date_str, "price": price\}\
    r = requests.post(WEBHOOK_URL, json=payload, timeout=30)\
    r.raise_for_status()\
    return r.json()\
\
def main():\
    pdf_url = find_latest_pdf_url()\
    pdf_bytes = http_get(pdf_url).content\
    text = extract_pdf_text(pdf_bytes)\
    date_str, price = pick_value_for_terminal(text)\
    if not date_str:\
        date_str = "1970-01-01 00:00"\
    resp = post_to_webapp(date_str, price)\
    print("Sent:", \{"date": date_str, "price": price\}, "Resp:", resp)\
\
if __name__ == "__main__":\
    main()\
}