#!/usr/bin/env python3
"""Urdu-script Pakistani address -> Roman letters.  One file, interactive, writes no files.

Run:  python urdu_to_roman.py      then type/paste an Urdu address and press Enter.
      (empty line, q, or Ctrl-C quits; piped input works too:  python urdu_to_roman.py < in.txt)

Rules handle address words (House, Block, Road...), letter names (ڈی ایچ اے -> DHA), numbers
and Latin text. The local model handles names, a run of words at a time. Every model answer is
checked with a consonant-skeleton score; doubtful answers are marked CHECK.  To fix one for good,
add it to MANUAL below.
"""
import copy, os, re, sys, time, unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

MODEL_NAME = "enstazao/Qalb-1.0-8B-Instruct"
SKELETON_THRESHOLD = 0.6
MANUAL = {   # your corrections, always win:  "اردو متن": "Roman Text",
}

SYSTEM_PROMPT = (
    "You transliterate Pakistani place and person names written in Urdu script into Roman "
    "letters, sound for sound. Never translate. Keep numbers and Latin letters unchanged. "
    "Write each word in Title Case. Reply with the Roman text only."
)

FEWSHOT = [
    ("کراچی", "Karachi"),
    ("اسلام آباد", "Islamabad"),
    ("گلشن اقبال", "Gulshan-e-Iqbal"),
    ("قائد اعظم", "Quaid-e-Azam"),
    ("مچھر", "Machhar"),
    ("کھوکھر", "Khokhar"),
    ("بھٹو", "Bhutto"),
    ("پٹھاناں", "Pathan"),
    ("نیو کراچی", "New Karachi"),
    ("شاہ فیصل", "Shah Faisal"),
    ("علامہ اقبال", "Allama Iqbal"),
    ("ڈیرہ اسماعیل خان", "Dera Ismail Khan"),
    ("ناظم آباد", "Nazimabad"),
    ("کوئٹہ", "Quetta"),
]

# ------------------------------------------------------------------ text cleaning
UR = "؀-ۿ"
CHAR_FIX = str.maketrans({"ي": "ی", "ى": "ی", "ې": "ی", "ۓ": "ے", "ك": "ک", "ه": "ہ", "ة": "ہ",
                          "ۂ": "ہ", "ۃ": "ہ", "أ": "ا", "إ": "ا", "ٱ": "ا"})
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
INVISIBLE = dict.fromkeys(map(ord, "​‌‍‎‏‪‫‬‭‮"
                                    "⁦⁧⁨⁩﻿"), None)
DIACRITICS = re.compile("[ً-ٰٟۖ-ۭـ]")
SEG_SPLIT = re.compile(r"[،,;؛۔|\n\r]+|\s[-–—]\s")
URDU_THEN_LATIN = re.compile(rf"(?<=[{UR}])\s*-?\s*(?=[0-9A-Za-z])")
LATIN_THEN_URDU = re.compile(rf"(?<=[0-9A-Za-z])\s*[/\-]?\s*(?=[{UR}])")
URDU_DOT = re.compile(rf"(?<=[{UR}])\.+")
LATIN_TOKEN = re.compile(r"[A-Za-z0-9/\-.'&]+")


def _pre(text) -> str:
    text = unicodedata.normalize("NFKC", "" if text is None else str(text))
    return DIACRITICS.sub("", text.translate(INVISIBLE).translate(CHAR_FIX).translate(DIGITS))


def clean_urdu(text) -> str:
    text = _pre(text).replace("#", " نمبر ")
    text = re.sub(r"[^\w/\-.'&\s]", " ", text)                      # brackets, ! ? : * @ ... -> space
    text = URDU_DOT.sub(" ", text)                                  # "نمبر." -> "نمبر"
    text = LATIN_THEN_URDU.sub(" ", URDU_THEN_LATIN.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip()


def split_segments(text):
    return [c for c in (clean_urdu(p) for p in SEG_SPLIT.split(_pre(text))) if c]


# ------------------------------------------------------------------ output convention
LOWER_PARTS = {"e", "o"}     # ezafe particles stay lowercase (only between two words)
ACRONYMS = {"DHA", "PECHS", "BCHS", "KDA", "LDA", "CDA", "FDA", "MDA", "HDA", "WAPDA", "PIA",
            "NED", "PAF", "CMH", "GPO", "GT", "FB", "NIPA", "PIB", "KMC", "HBL", "UBL", "MCB",
            "ABL", "NBP", "SSGC", "KESC", "PTCL", "KPK", "AJK", "ATM"}


def _hyphen_title(part: str) -> str:
    sub, out = part.split("-"), []
    for i, p in enumerate(sub):
        if not p:
            out.append(p)
        elif 0 < i < len(sub) - 1 and p.lower() in LOWER_PARTS:
            out.append(p.lower())
        elif p.upper() in ACRONYMS:
            out.append(p.upper())
        elif p.isdigit():
            out.append(p)
        elif len(p) == 1:
            out.append(p.upper())
        elif p.isupper() and len(p) <= 4:
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:].lower())
    return "-".join(out)


def normalize_label(s: str) -> str:
    """ASCII, Title Case, no diacritics, acronyms kept, ezafe -e- lowercase."""
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[\"'’`]", "", s)
    s = re.sub(r"[^A-Za-z0-9/\- ]", " ", s)
    s = re.sub(r"(?<![A-Za-z0-9])-|-(?![A-Za-z0-9])", " ", s)            # stray hyphens
    words = s.split()
    if (len(words) > 1 and all(w.isupper() or w.isdigit() for w in words)
            and any(w.isalpha() and len(w) >= 4 and w not in ACRONYMS for w in words)):
        words = [w.lower() if w.isalpha() else w for w in words]         # model shouted: un-shout
    return " ".join("/".join(_hyphen_title(p) for p in w.split("/")) for w in words)


def _join_ezafe(s: str) -> str:
    """'Gulshan E Iqbal' -> 'Gulshan-e-Iqbal' (word, lone E, word)."""
    return re.sub(r"\b([A-Za-z]{2,}) [Ee] ([A-Za-z]{2,})\b", r"\1-e-\2", s)


# ------------------------------------------------------------------ rule layer
def _build(src: str) -> dict:
    d = {}
    for line in src.strip().splitlines():
        urdu, roman = line.split("=")
        for v in urdu.split("|"):
            d[clean_urdu(v)] = normalize_label(roman)
    return d


LEX = _build("""
ہاؤس|ہائوس|ہاوس|ھاؤس = House
نمبر = Number
بلاک = Block
مکان = House
گلی = Gali
دکان = Dukan
پلاٹ = Plot
فلیٹ = Flat
فلیٹس = Flats
شاپ = Shop
سیکٹر = Sector
فیز = Phase
روڈ = Road
سروس = Service
مین = Main
اسٹریٹ|سٹریٹ = Street
لین = Lane
ایونیو = Avenue
ایریا|ایریہ = Area
ٹاؤن|ٹاون|ٹائون = Town
ٹاؤن شپ|ٹاون شپ|ٹاؤنشپ = Township
کالونی = Colony
سوسائٹی = Society
ہاؤسنگ = Housing
مارکیٹ = Market
بازار = Bazaar
چوک = Chowk
چورنگی = Chowrangi
موڑ = Mor
گوٹھ = Goth
محلہ = Mohalla
نزد = Nazdeek
مسجد = Masjid
پلازہ = Plaza
ٹاور = Tower
اپارٹمنٹ = Apartment
اپارٹمنٹس = Apartments
بلڈنگ = Building
وارڈ = Ward
یونٹ = Unit
سیکشن = Section
بس اسٹاپ|بس سٹاپ = Bus Stop
اسٹاپ|سٹاپ = Stop
ہسپتال|اسپتال = Hospital
اسکول|سکول = School
کالج = College
یونیورسٹی = University
پارک = Park
بینک = Bank
اسٹیشن|سٹیشن = Station
ریلوے = Railway
ہائی وے|ہائیوے = Highway
موٹر وے|موٹروے = Motorway
فلائی اوور = Flyover
انڈر پاس = Underpass
چک = Chak
ڈھوک = Dhok
موضع = Mauza
تحصیل = Tehsil
ضلع = Zila
پوسٹ آفس = Post Office
بستی = Basti
گاؤں|گاوں = Gaon
کوٹ = Kot
خیابان=Khayaban
سمندر خان=Samdar Khan
منگو پیر=Manghopir
کینجھر=Kinjhar
گاڑی کھاتہ=Gari Khata
""")
MAX_PHRASE = max(len(k.split()) for k in LEX)

LETTERS = {clean_urdu(k): v for k, v in {
    "اے": "A", "بی": "B", "سی": "C", "ڈی": "D", "ای": "E", "ایف": "F", "جی": "G", "ایچ": "H",
    "آئی": "I", "جے": "J", "کے": "K", "ایل": "L", "ایم": "M", "این": "N", "او": "O", "پی": "P",
    "کیو": "Q", "آر": "R", "ایس": "S", "ٹی": "T", "یو": "U", "وی": "V", "ڈبلیو": "W",
    "ایکس": "X", "وائی": "Y", "زیڈ": "Z", "زی": "Z"}.items()}
BLOCK_WORDS = {clean_urdu(w) for w in "بلاک سیکٹر لین وارڈ یونٹ سیکشن فیز اسٹریٹ سٹریٹ ایونیو گلی".split()}
SINGLE = {"ا": "A", "ب": "B", "پ": "P", "ت": "T", "ٹ": "T", "ج": "J", "چ": "C", "د": "D",
          "ڈ": "D", "ر": "R", "ڑ": "R", "ز": "Z", "س": "S", "ش": "S", "ع": "A", "غ": "G",
          "ف": "F", "ق": "Q", "ک": "K", "گ": "G", "ل": "L", "م": "M", "ن": "N", "و": "W",
          "ہ": "H", "ی": "Y"}                                    # chak suffixes: "45 ج ب" -> "45 J B"
BIBI = [clean_urdu("بی")] * 2                                    # "بی بی" is Bibi, not "BB"


def _letter_run(tokens, i, numeric):
    """Letter names: 2+ in a row -> acronym (DHA); 1 -> letter after Block/Sector.., before a
    number, or after a number at the end of the segment (85 بی -> 85 B)."""
    n, j = len(tokens), i
    while j < n and tokens[j] in LETTERS:
        j += 1
    run = tokens[i:j]
    if len(run) >= 2 and run != BIBI:
        return "".join(LETTERS[t] for t in run), j
    if len(run) == 1 and ((i > 0 and tokens[i - 1] in BLOCK_WORDS)
                          or (j < n and tokens[j][:1].isdigit()) or (numeric and j == n)):
        return LETTERS[run[0]], j
    return None


def _plan(tokens):
    """-> [('done', roman) | ('todo', urdu_token)]. Pure rules, no model."""
    out, i, n, numeric = [], 0, len(tokens), False
    while i < n:
        tok = tokens[i]
        hit = None
        for k in range(min(MAX_PHRASE, n - i), 0, -1):
            ph = " ".join(tokens[i:i + k])
            if ph in LEX:
                hit = (LEX[ph], k)
                break
        if hit:
            out.append(("done", hit[0])); i += hit[1]; numeric = False
        elif LATIN_TOKEN.fullmatch(tok):
            out.append(("done", normalize_label(tok))); i += 1; numeric = tok[:1].isdigit()
        elif tok in LETTERS and (lr := _letter_run(tokens, i, numeric)):
            out.append(("done", lr[0])); i = lr[1]; numeric = False
        elif numeric and len(tok) == 1 and tok in SINGLE:
            out.append(("done", SINGLE[tok])); i += 1
        else:
            out.append(("todo", tok)); i += 1; numeric = False
    return out


# ------------------------------------------------------------------ consonant-skeleton check
URDU_CLASS = {c: cls for chars, cls in (("بپف", "b"), ("تٹطدڈ", "t"), ("ثسصزذضظژشچ", "s"), ("ج", "j"),
                                         ("گغقکخ", "k"), ("م", "m"), ("نں", "n"), ("ل", "l"), ("رڑ", "r"))
              for c in chars}          # weak letters ا آ و ی ے ء ع ہ ھ ح ؤ ئ are ignored on purpose
_ROMAN_CLASS = str.maketrans({"q": "k", "c": "k", "x": "k", "g": "k", "z": "s", "d": "t",
                              "p": "b", "f": "b"})
_DIGRAPHS = (("chh", "s"), ("ch", "s"), ("sh", "s"), ("zh", "s"), ("kh", "k"), ("gh", "k"),
             ("th", "t"), ("dh", "t"), ("ph", "b"), ("bh", "b"), ("jh", "j"), ("rh", "r"))


def roman_skeleton(s: str) -> str:
    s = re.sub(r"[^a-z]", "", s.lower())
    for a, b in _DIGRAPHS:
        s = s.replace(a, b)
    return re.sub(r"[aeiouyhwv]", "", s).translate(_ROMAN_CLASS)       # v/w = Urdu 'و' (ignored)


def urdu_skeleton(segment: str) -> str:
    return "".join(roman_skeleton(t) if LATIN_TOKEN.fullmatch(t) else "".join(URDU_CLASS.get(c, "") for c in t)
                   for t in segment.split())


def skeleton_score(segment: str, roman: str) -> float:
    a, b = urdu_skeleton(segment), roman_skeleton(roman)
    return 1.0 if not a and not b else SequenceMatcher(None, a, b, autojunk=False).ratio()


# ------------------------------------------------------------------ model
# Speed: the system prompt + 14 examples are identical on every call, so they are run through
# the model ONCE and their attention cache is reused (same output, far less CPU work per call).
# Identical inputs are also never sent to the model twice (greedy decoding is deterministic).
_M = {}


def _msgs(user_text=None):
    m = [{"role": "system", "content": SYSTEM_PROMPT}]
    for u, r in FEWSHOT:
        m += [{"role": "user", "content": u}, {"role": "assistant", "content": r}]
    if user_text is not None:
        m.append({"role": "user", "content": user_text})
    return m


def _enc(s):
    return _M["tok"]([s], return_tensors="pt", add_special_tokens=False).input_ids


def _build_prefix():
    """Cache the shared prompt prefix; leave it off (None) if it cannot be proven identical."""
    import torch
    _M["prefix"] = None
    try:
        tok = _M["tok"]
        text = tok.apply_chat_template(_msgs(), tokenize=False)
        probe = tok.apply_chat_template(_msgs("x"), tokenize=False, add_generation_prompt=True)
        if not probe.startswith(text):
            return
        p_ids = _enc(text)
        if not torch.equal(torch.cat([p_ids, _enc(probe[len(text):])], 1), _enc(probe)):
            return
        with torch.inference_mode():
            _M["prefix"] = (text, p_ids, _M["model"](input_ids=p_ids, use_cache=True).past_key_values)
    except Exception:
        _M["prefix"] = None


def _prepare():
    if "model" in _M:
        return
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    torch.set_num_threads(os.cpu_count() or 4)
    print("Loading model on CPU (1-2 minutes for an 8B model)...", flush=True)
    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    if not tok.chat_template:
        tok.chat_template = (
            "{{ bos_token }}{% for message in messages %}"
            "{{ '<|start_header_id|>' + message['role'] + '<|end_header_id|>\n\n' "
            "+ message['content'] | trim + '<|eot_id|>' }}{% endfor %}"
            "{% if add_generation_prompt %}"
            "{{ '<|start_header_id|>assistant<|end_header_id|>\n\n' }}{% endif %}")
    try:
        model = AutoModelForCausalLM.from_pretrained(MODEL_NAME, dtype=torch.bfloat16, low_cpu_mem_usage=True)
    except TypeError:                                   # older transformers
        model = AutoModelForCausalLM.from_pretrained(MODEL_NAME, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True)
    model.eval()
    ids = [tok.eos_token_id, tok.convert_tokens_to_ids("<|eot_id|>"), tok.convert_tokens_to_ids("<|end_of_text|>")]
    _M.update(tok=tok, model=model, stop=[i for i in dict.fromkeys(ids) if i is not None and i != tok.unk_token_id])
    _build_prefix()


@lru_cache(maxsize=None)
def llm_text(user_text: str) -> str:
    """One greedy model call on a run of name words -> normalised Roman text."""
    import torch
    _prepare()
    tok, model = _M["tok"], _M["model"]
    full = tok.apply_chat_template(_msgs(user_text), tokenize=False, add_generation_prompt=True)
    pre = _M.get("prefix")
    use_cache = bool(pre) and full.startswith(pre[0])
    ids = torch.cat([pre[1], _enc(full[len(pre[0]):])], 1) if use_cache else _enc(full)
    opts = dict(attention_mask=torch.ones_like(ids), max_new_tokens=min(96, 14 * len(user_text.split()) + 16),
                do_sample=False, eos_token_id=_M["stop"], pad_token_id=tok.eos_token_id)
    with torch.inference_mode():
        try:
            out = model.generate(input_ids=ids, **opts,
                                 **({"past_key_values": copy.deepcopy(pre[2])} if use_cache else {}))
        except Exception:
            if not use_cache:
                raise
            _M["prefix"] = None                          # cache misbehaves on this setup: stop using it
            ids = _enc(full)
            out = model.generate(input_ids=ids, **{**opts, "attention_mask": torch.ones_like(ids)})
    text = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
    return normalize_label(_join_ezafe(normalize_label(text.splitlines()[0] if text else "")))


# ------------------------------------------------------------------ core logic
OVERRIDES = {clean_urdu(k): v for k, v in MANUAL.items()}


def _best_of(run: str):
    cand = llm_text(run)
    score = skeleton_score(run, cand) if cand else 0.0
    words = run.split()
    if (score < SKELETON_THRESHOLD or not cand) and len(words) > 1:    # retry word by word
        parts = [llm_text(w) for w in words]
        # a word the model could not do stays visible in Urdu (never silently dropped)
        alt = normalize_label(" ".join(parts)) if all(parts) else " ".join(p or w for p, w in zip(parts, words))
        alt_score = skeleton_score(run, alt) if alt else 0.0
        if alt_score > score:
            cand, score = alt, alt_score
    return cand, score


@lru_cache(maxsize=None)
def _resolve(run: str):
    """-> (roman, score, ok). Remembered for the session, so repeats cost nothing."""
    best, score = _best_of(run)
    ok = bool(best) and score >= SKELETON_THRESHOLD and not re.search(f"[{UR}]", best)
    return best or run, round(score, 2), ok


def roman_segment(seg: str):
    """-> (roman, doubts) ; doubts = [(urdu_run, roman_guess, score)]"""
    if seg in OVERRIDES:
        return OVERRIDES[seg], []
    pieces, doubts, run = [], [], []

    def flush():
        if run:
            r = " ".join(run)
            if r in OVERRIDES:
                pieces.append(OVERRIDES[r])
            else:
                roman, score, ok = _resolve(r)
                pieces.append(roman)
                if not ok:
                    doubts.append((r, roman, score))
            run.clear()

    for kind, val in _plan(seg.split()):
        if kind == "todo":
            run.append(val)
        else:
            flush(); pieces.append(val)
    flush()
    return " ".join(p for p in pieces if p), doubts


def transliterate(text):
    """-> (roman_address, doubts)"""
    outs, doubts = [], []
    for seg in split_segments(text):
        r, d = roman_segment(seg)
        outs.append(r); doubts += d
    return ", ".join(outs), doubts


def transliterate_address(text) -> str:
    return transliterate(text)[0]


# ------------------------------------------------------------------ interactive loop
def main():
    for s in (sys.stdin, sys.stdout):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    tty = sys.stdin.isatty()
    _prepare()                                           # load now, so the first address is fast
    if tty:
        print("Ready. Type or paste an Urdu address and press Enter (empty line to quit).")
    while True:
        try:
            line = input("\n> " if tty else "").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line.lower() in {"q", "quit", "exit"} or (tty and not line):
            break
        if not line:
            continue
        t0 = time.perf_counter()
        roman, doubts = transliterate(line)
        print(f"{roman}" + ("   (CHECK)" if doubts else "") + f"   [{time.perf_counter() - t0:.1f}s]")
        for run, guess, score in doubts:
            print(f'   check: "{run}" -> "{guess}" (score {score}); add to MANUAL if wrong')


if __name__ == "__main__":
    main()