def smart_find_numbers(segment: str):
    """
    Grąžina tik 'piniginius' skaičius su 2 skaitmenimis po kablelio.
    Taiso sujungimus (pvz. '1054.20541.20' -> '1054.20 541.20').
    Leidžia tūkstančių tarpus arba NBSP.
    """
    # ištaisom sujungimus tarp piniginių skaičių
    segment = re.sub(r"(\d[.,]\d{2})(?=\d)", r"\1 ", segment)

    # pagaunam tik skaičius su dviem skaitmenimis po kablelio
    money_pat = re.compile(
        r"(?<!\d)"                                 # ne prieš skaitmenį
        r"(\d{1,3}(?:[ \u00A0]\d{3})*|\d+)"        # 1-3 sk. + tūkst. grupės arba vientisas skaičius
        r"[.,]\d{2}"                               # kablelis/taškas + 2 skaitmenys
        r"(?!\d)"                                  # po to ne skaitmuo
    )

    matches = money_pat.findall(segment)
    # money_pat su grupėm – susikonstruojam pilną atitikmenį per re.finditer
    vals = []
    for m in re.finditer(money_pat, segment):
        token = m.group(0)
        try:
            token = token.replace("\u00A0", " ").replace(" ", "")
            token = token.replace(",", ".")
            val = float(token)
            if 0 < val < 10000:    # normalus intervalas
                vals.append(val)
        except Exception:
            continue
    return vals


def pick_from_text_in_block(pdf_text: str):
    """
    Teksto režimas: imame tik bloką po TERMINAL_HDR iki kito skyriaus.
    Toje atkarpoje randame eilutę su produktu ir iš jos (plius max 2 sek. eilutės)
    paimame TREČIĄ piniginį skaičių (xx.xx).
    """
    # data
    mdate = DATE_RE.search(pdf_text)
    date_str = f"{mdate.group(1)} {mdate.group(2)}" if mdate else None

    lines = pdf_text.splitlines()

    # startas – terminalo antraštė
    start = None
    for i, l in enumerate(lines):
        if TERMINAL_HDR in l:
            start = i + 1
            break
    if start is None:
        raise RuntimeError("Tekste neradau terminalo antraštės.")

    # pabaiga – kitas terminalo blokas (UAB/AB ... 'terminalas')
    end = len(lines)
    for j in range(start, len(lines)):
        t = lines[j].strip()
        if (t.startswith("UAB ") or t.startswith("AB ")) and "terminalas" in t:
            end = j
            break

    block = lines[start:end]

    # randame TIK produkto eilutę
    idx = None
    for k, l in enumerate(block):
        if l.strip().startswith(PRODUCT_PREFIX):
            idx = k
            break
    if idx is None:
        raise RuntimeError("Bloke neradau produkto eilutės.")

    # paimam produkto eilutę + dar iki 2 sek. eilučių (jei PDF 'laužo' stulpelius)
    window = " ".join(block[idx: idx + 3])

    nums = smart_find_numbers(window)
    if len(nums) < 3:
        raise RuntimeError(f"Per mažai piniginių skaičių produkto eilutėje: {nums}")

    price = round(nums[2], 2)   # BŪTENT trečias piniginis skaičius
    return date_str, price
