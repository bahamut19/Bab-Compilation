#!/usr/bin/env python3
"""
Build a Quarto book from the Bab's writings .md source files.

    python3 tools/build_book.py --src source --out .

Outputs (all regenerated on every run):
  chapters/<part>/*.qmd     one file per writing (+ one title page per source volume)
  _quarto.yml               YOUR settings (created once, never overwritten)
  _quarto-web.yml           chapter list for the HTML site (generated)
  _quarto-docx-*.yml        one Word edition per profile, each under the word limit (generated)
  tools/render_all.py       renders the site and every DOCX edition (cross-platform)
  tools/review_report.md    everything that still needs a human decision

Optional hand-edited override files (never overwritten):
  tools/titles.json         {"BB00577": "Prayer for ..."}
  tools/headings.json       {"BB00001": [{"match": "first words of paragraph", "level": 2, "text": "Heading"}]}
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

PARTS = [
    # key, part title (sidebar), first year, last year, short label (file names / edition titles)
    ("part1", "Writings Revealed 1844–1846", 1844, 1846, "1844-1846"),
    ("part2", "Writings Revealed in 1847 — The Year of the Bayan", 1847, 1847, "1847"),
    ("part3", "Writings Revealed 1848–1850", 1848, 1850, "1848-1850"),
]
UNDATED = ("part0", "Writings Without Known Dates", 0, 0, "Undated")

LONG_DOC_WORDS = 2500
SPLIT_PARA_WORDS = 220
TARGET_CHUNK_WORDS = 120
EDITION_WORD_LIMIT = 450_000     # Gemini Notebook allows 500,000 words per source; leave a safety margin
MIN_EMBEDDED_QUOTE_WORDS = 12
GAP_WORDS = 6000                 # sections longer than this get sub-parts
GAP_TARGET = 3000
SHORT = 16                       # max words for a line to count as a possible heading

SEP_RE = re.compile(r"^\s*={5,}\s*$")
HEAD_RE = re.compile(r"^(BB[A-Za-z]*\d+)\b[ \t]*(.*)$")
KEEP_ID_RE = re.compile(r"^BB\d+$")
PLACEHOLDER_RE = re.compile(r"text to be (located|typed)", re.I)
META_RE = re.compile(r"^(To|Date|From|Place|Addressee|Recipient|Written)\s*:\s*\S", re.I)
BASMALA_RE = re.compile(r"^(in (the|his) name|he is\b)", re.I)


def words(t):
    return len(t.split())


# ---------------------------------------------------------------- text cleanup
def fix_encoding(s):
    s = s.replace("ImÃims", "Imams")
    s = s.replace("Ãi", "a")
    s = re.sub(r"Ã(?![A-Za-z])", "a", s)
    return s


import unicodedata

ITAL_OPEN, ITAL_CLOSE = "\ue000", "\ue001"


def _braced(s, start):
    """index of the } matching the { at s[start]"""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def latex_to_md(s):
    r"""\footnote{..} -> ^[..]; \textit{..} -> italics; \d{h} / \u{a} -> accented letters."""
    s = re.sub(r"\\d\{(.)\}", lambda m: unicodedata.normalize("NFC", m.group(1) + "\u0323"), s)
    s = re.sub(r"\\u\{(.)\}", lambda m: unicodedata.normalize("NFC", m.group(1) + "\u0306"), s)
    for cmd in ("footnote", "textit"):
        key = "\\" + cmd + "{"
        while True:
            i = s.find(key)
            if i < 0:
                break
            j = _braced(s, i + len(key) - 1)
            if j < 0:
                s = s[:i] + s[i + len(key):]
                continue
            inner = latex_to_md(s[i + len(key):j])
            if cmd == "footnote":
                inner = inner.replace("[", "\\[").replace("]", "\\]")
                rep = "^[" + inner + "]"
            else:
                rep = ITAL_OPEN + inner + ITAL_CLOSE
            s = s[:i] + rep + s[j + 1:]
    return s


def strip_footnotes(s):
    while True:
        i = s.find("\\footnote{")
        if i < 0:
            return s
        j = _braced(s, i + len("\\footnote{") - 1)
        s = s[:i] + (s[j + 1:] if j >= 0 else s[i + 10:])


def clean_text(s):
    s = latex_to_md(fix_encoding(s.replace("\u00a0", " ")))
    s = s.replace("`", "ʿ")
    s = s.replace("---", "—")
    s = re.sub(r"\s*--\s*", "—", s)
    s = re.sub(r"\s-\s", "—", s)
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    s = s.replace("*", r"\*").replace("_", r"\_")
    s = s.replace(ITAL_OPEN, "*").replace(ITAL_CLOSE, "*")
    s = re.sub(r"^#", r"\#", s)
    s = re.sub(r"^(\d+)\.", r"\1\\.", s)
    s = re.sub(r"^([-+>])", r"\\\1", s)
    return s


def plain(s):
    """clean_text without markdown escaping (for headings / titles)."""
    return clean_text(s).replace("\\*", "*").replace("\\_", "_").replace("\\#", "#").replace("\\.", ".")


def split_sentences(p):
    parts = re.split(r'(?<=[.!?])(["”’)\]]?)\s+(?=["“‘(A-Z\\])', p)
    out, i = [], 0
    while i < len(parts):
        seg = parts[i]
        if i + 1 < len(parts):
            seg += parts[i + 1]
            i += 2
        else:
            i += 1
        out.append(seg)
    return out


def split_long_paragraph(p):
    if words(p) <= SPLIT_PARA_WORDS:
        return [p]
    chunks, cur, cw = [], [], 0
    for s in split_sentences(p):
        cur.append(s)
        cw += words(s)
        if cw >= TARGET_CHUNK_WORDS:
            chunks.append(" ".join(cur))
            cur, cw = [], 0
    if cur:
        if chunks and cw < 35:
            chunks[-1] += " " + " ".join(cur)
        else:
            chunks.append(" ".join(cur))
    return chunks


# ---------------------------------------------------------------- quotations
EMBEDDED_QUOTE_RE = re.compile(r'^(?P<lead>.*?[:,]) "(?P<quote>[^"]+)"(?P<rest>(?:\s.*)?)$', re.S)


def split_embedded_quote(raw):
    if raw.count('"') != 2:
        return None
    m = EMBEDDED_QUOTE_RE.match(raw.strip())
    if not m or words(m.group("quote")) < MIN_EMBEDDED_QUOTE_WORDS or words(m.group("lead")) < 2:
        return None
    return m.group("lead").strip(), '"' + m.group("quote").strip() + '"', m.group("rest").strip()


def is_whole_quote(p):
    q = p.strip()
    return q.startswith('"') and q.count('"') == 2 and q.endswith(('"', '."', '!"'))


# ---------------------------------------------------------------- headings
PRAYER_LEAD_RE = re.compile(r"^(?:And\s+)?(?:this\s+was\s+)?(?:among\s+)?(?:his|His)?\s*(prayers?)\b(.*)$", re.I)
MONTH_RE = re.compile(r"^\((\d+)\.\s*(.+?)\)$")
ORD_CHAPTER_RE = re.compile(r"^((?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth)\s+chapter)\s*:\s*(.+)$", re.I)
SURAH_RE = re.compile(r"^Surah of [^.]{2,70}$")
GATE_RE = re.compile(r"^Gate\s+(\S+)\s+of\s+the\s+(\S+)\s+Unity\b[\s,.:;]*$", re.I)
CHAPTER_RE = re.compile(r"^[\[(]?\s*(Chapter|Book)\s+([A-Za-z\-]+|\d+)\s*[\])]?\s*(?:[,:]\s*(.*))?$", re.I)
BRACKET_RE = re.compile(r"^\[(?:(\d+)\.\s*)?([^\[\]]+?)\]$")
PAREN_RE = re.compile(r"^\(([^()]+?)\)$")
WEEKDAY_RE = re.compile(r"^(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)\s*:?$", re.I)
PRAYER_TITLE_RE = re.compile(r"^(?:His |A |The )?(?:Prayer|Supplication)\b[^.]*:$", re.I)
ORDINAL_UNIT_RE = re.compile(r"^The\s+\w+\s+(Vahid|Unity|Gate|Chapter|Section|Book|Part)\s*[.:]?$", re.I)
DATED_PRAYER_RE = re.compile(r"^(?:A\s+)?(?:Prayer|Du'a'?|Supplication)\b[^.!?:]{0,140}\b(?:Year|Night|Day|Month|Hour)s?\b[^.!?:]{0,80}$", re.I)
VAHID_RE = re.compile(r"^\[Vahid\s+(\d+),\s*Bab\s+(\d+)(?:,\s*([^\]]+))?\]$", re.I)
VAHID_MONTH_RE = re.compile(r"^The\s+\w+\s+(?:Vahid|month),\s*the month of\s+.+?:?$", re.I)
FOOT_TAIL_RE = re.compile(r"^(.*?)\s*\\footnote\{(.*)\}\s*$", re.S)
SUBTITLE_START_RE = re.compile(r"^(concerning|on|in explanation|explaining|regarding|about)\b", re.I)


def up1(s):
    s = s.strip().rstrip(":;,. ")
    return s[:1].upper() + s[1:] if s else s


def title_words(s):
    return " ".join(w[:1].upper() + w[1:] if i == 0 else w for i, w in enumerate(s.split()))


def detect(raw, nxt):
    """Return (level, text, consumed_next_line, unity) or None."""
    r = raw.strip()
    w = words(r)
    if w > SHORT + 18 or not r:
        return None
    m = MONTH_RE.match(r)
    if m:
        n = m.group(2).strip()
        return 2, ("The " + n if n.lower().startswith("month") else f"{m.group(1)}. {up1(n)}"), 0, None
    m = ORD_CHAPTER_RE.match(r)
    if m:
        return 2, f"{m.group(1).title()}: {m.group(2).strip()}", 0, None
    if r.endswith(":") and w <= 32 and re.match(r"^(And|Among|This)\b", r) and re.search(r"\bprayers?\b", r, re.I):
        m = PRAYER_LEAD_RE.match(r.rstrip(":").strip())
        if m:
            rest = m.group(2).strip()
            rest = re.sub(r"^was\s+this\s+one\s+", "", rest, flags=re.I)
            rest = re.sub(r"\s+was(\s+this)?$", "", rest, flags=re.I)
            rest = re.sub(r"^was\s+", "", rest, flags=re.I)
            rest = re.sub(r"\s*\((?:peace|blessings)[^)]*\)", "", rest, flags=re.I).strip()
            return 2, ("Prayer " + rest).strip(), 0, None
    if w > SHORT:
        return None
    if ORDINAL_UNIT_RE.match(r):
        return 2, up1(r), 0, None
    if DATED_PRAYER_RE.match(r) and 4 <= w <= 30:
        return 2, up1(r), 0, None
    m = VAHID_RE.match(r)
    if m:
        name = (m.group(3) or "").strip()
        return 3, f"Bab {m.group(2)}" + (f": {up1(name)}" if name else ""), 0, f"Vahid {m.group(1)}"
    if VAHID_MONTH_RE.match(r):
        return 2, up1(r), 0, None
    m = GATE_RE.match(r)
    if m:
        text = f"Gate {m.group(1).title()} of the {m.group(2).title()} Unity"
        consumed = 0
        if nxt and words(nxt) <= 22 and not BASMALA_RE.match(nxt) and (nxt[:1].islower() or SUBTITLE_START_RE.match(nxt)):
            text += ": " + up1(nxt)
            consumed = 1
        return 3, text, consumed, f"The {m.group(2).title()} Unity"
    if SURAH_RE.match(r) and w <= 10:
        return 2, r, 0, None
    m = CHAPTER_RE.match(r)
    if m and not r.endswith("."):
        text = f"{m.group(1).title()} {m.group(2).title() if not m.group(2).isdigit() else m.group(2)}"
        tail = (m.group(3) or "").strip()
        consumed = 0
        if tail:
            text += ": " + up1(tail)
        elif nxt and words(nxt) <= 14 and SUBTITLE_START_RE.match(nxt):
            text += ": " + up1(nxt)
            consumed = 1
        return 2, text, consumed, None
    m = BRACKET_RE.match(r)
    if m and re.search(r"[A-Za-z]", m.group(2)) and not re.search(r"\bQ\d+:\d+|\d+:\d+|translation|\bnot available\b", m.group(2)) and w <= 14:
        return (2 if m.group(1) else 3), (f"{m.group(1)}. " if m.group(1) else "") + up1(m.group(2)), 0, None
    m = PAREN_RE.match(r)
    if m and re.match(r"[A-Za-z]", m.group(1)) and not re.search(r"\b(peace|blessings|salutations?|may )\b", m.group(1), re.I) and w <= 14:
        return 3, up1(m.group(1)), 0, None
    if WEEKDAY_RE.match(r):
        return 2, up1(r), 0, None
    if PRAYER_TITLE_RE.match(r) and w <= 12:
        return 2, up1(r), 0, None
    return None



# ---------------------------------------------------------------- automatic headings (long writings with no markers)
VERSE_LEAD_RE = re.compile(r'^(?:And\s+)?(?:God|the Almighty)\b[^":“]{0,50}\b(?:said|saith|says|declared)\s*:?\s*["“]', re.I)
QUESTION_LEAD_RE = re.compile(
    r"^(?:And\s+)?(?:As\s+(?:to|for|regarding)|Concerning|Regarding)\s+(?:that\s+which|what|the\s+(?:question|matter))\s+"
    r"(?:thou|you|ye)\s+(?:didst|hast|have|did)?\s*(?:ask(?:ed)?|inquire[d]?|enquire[d]?)\s*(?:concerning|about|regarding|of|touching)?\s*(?P<t>.*)$", re.I)
TRAIL_STOP = {"which", "that", "of", "and", "the", "in", "to", "a", "an", "is", "as", "for", "by", "with", "from", "on"}


def _trim_words(text, n):
    ws = text.split()
    cut = len(ws) > n
    ws = ws[:n]
    while ws and ws[-1].lower().strip(",.;:") in TRAIL_STOP:
        ws.pop()
    out = " ".join(ws).strip(" ,.;:—-\"“”")
    return out, cut


def incipit_chunks(body, a, b, target_words, level):
    """Split body[a:b] into even parts at paragraph boundaries; name each by its opening words."""
    total = sum(words(l) for l in body[a:b])
    n = max(2, round(total / target_words))
    target = total / n
    starts, acc = [a], 0
    for i in range(a, b):
        acc += words(body[i])
        if acc >= target * len(starts) and i + 1 < b and len(starts) < n:
            j = i + 1
            if words(body[i]) <= 15 and i > starts[-1]:     # keep a lone basmala with its text
                j = i
            starts.append(j)
    heads = {}
    for k, st in enumerate(starts, 1):
        j = st
        while j < b - 1 and words(body[j]) <= 15 and BASMALA_RE.match(body[j].strip()):
            j += 1
        inc, cut = _trim_words(re.sub(r'^["“]|^Say:\s*', "", body[j].strip()), 8)
        heads[st] = (level, f"Part {k}: “{inc}…”")
    return heads


def auto_headings(body, kind_hint=None):
    """Return ({idx: (level, text)}, kind) for a long writing that has no explicit section markers."""
    verse = {}
    for i, l in enumerate(body):
        if words(l) <= 70 and VERSE_LEAD_RE.match(l.strip()):
            m = re.search(r'["“](.+?)["”]', l)
            if m:
                q, cut = _trim_words(m.group(1), 9)
                if q:
                    verse[i] = (2, f"On the verse “{q}{'…' if cut else ''}”")
    if len(verse) >= 3:
        return verse, "verse"
    quest = {}
    for i, l in enumerate(body):
        m = QUESTION_LEAD_RE.match(l.strip()) if words(l) <= 400 else None
        if m:
            t_full = m.group("t")
            t, cut = _trim_words(re.split(r'\s*(?:—|--|["“])', t_full)[0], 10)
            qm = re.search(r'["“]([^"”]{5,})["”]', t_full)
            if qm and len(t.split()) <= 3:
                qt, qcut = _trim_words(qm.group(1), 8)
                t = f"{t} “{qt}{'…' if qcut else ''}”".strip()
            quest[i] = (2, "On " + t if t else "Reply to a question")
    if len(quest) >= 3:
        return quest, "question"
    # incipit parts
    return incipit_chunks(body, 0, len(body), 2500, 2), "incipit"

# ---------------------------------------------------------------- one writing
def build_doc_qmd(doc, title_override=None, heading_overrides=None):
    body = list(doc["lines"])
    meta = []
    while body and META_RE.match(body[0].strip()):
        meta.append(body.pop(0).strip())

    title = (title_override or doc["title"] or "").strip()
    derived = False
    if not title:
        for mline in meta:
            if mline.lower().startswith("to:"):
                t = re.sub(r"\s*\?+\s*$", "", mline[3:].strip())
                title, derived = "To " + t, True
                break
    if not title and body and words(body[0]) <= 8 and re.match(r"^(A |An |The )?(Prayer|Tablet|Letter|Epistle|Commentary|Treatise|Answer|Sermon|Supplication|Tafsir|Khutba)\b", body[0].strip()):
        title, derived = body.pop(0).strip().rstrip(":."), True

    h1 = f"{doc['id']}: {plain(title)}" if title else doc["id"]
    out = [f"# {h1} {{.unnumbered}}", ""]
    for mline in meta:
        out += [f"*{plain(mline)}*", ""]

    # ---- find markers
    markers, notes, i = {}, {}, 0
    while i < len(body):
        nxt = body[i + 1].strip() if i + 1 < len(body) else ""
        line = body[i]
        ft = FOOT_TAIL_RE.match(line.strip())
        d = detect(ft.group(1), nxt) if (ft and words(ft.group(1)) <= 14) else detect(line, nxt)
        if d:
            if ft and words(ft.group(1)) <= 14:
                notes[i] = ft.group(2).strip()
            markers[i] = d
            i += 1 + d[2]
        else:
            i += 1
    consumed = {i + 1 for i, d in markers.items() if d[2]}

    # manual overrides: insert before first paragraph that starts with `match`
    inserts = {}
    for ov in heading_overrides or []:
        for j, l in enumerate(body):
            if l.strip().startswith(ov["match"]):
                inserts.setdefault(j, []).append((int(ov.get("level", 2)), ov["text"]))
                break

    auto_kind = None
    if not markers and not inserts and sum(words(l) for l in body) > LONG_DOC_WORDS:
        ah, auto_kind = auto_headings(body)
        for i2, (lv2, tx2) in ah.items():
            inserts.setdefault(i2, []).append((lv2, tx2))
    # subdivide any section that is still very long
    hl = {i2: d[0] for i2, d in markers.items()}
    for i2, v in inserts.items():
        hl[i2] = min([hl.get(i2, 9)] + [lv for lv, _ in v])
    edges = sorted(set([0] + list(hl) + [len(body)]))
    gap_added = 0
    for a, b in zip(edges, edges[1:]):
        if sum(words(l) for l in body[a:b]) > GAP_WORDS and b - a >= 4:
            parent = hl.get(a, 2)
            for i2, (lv2, tx2) in incipit_chunks(body, a if a not in hl else a + 1, b, GAP_TARGET, min(parent + 1, 4)).items():
                inserts.setdefault(i2, []).append((lv2, tx2))
                gap_added += 1
    levels = [d[0] for d in markers.values()] + [lv for v in inserts.values() for lv, _ in v]
    if any(d[3] for d in markers.values()):
        levels.append(2)          # group headings (Unity / Vahid) are generated at level 2
    shift = (min(levels) - 2) if levels else 0

    total_words = sum(words(l) for l in body)
    first = min(list(markers) + list(inserts)) if (markers or inserts) else None
    pre_words = sum(words(l) for l in body[:first]) if first else 0
    stats = dict(gap=gap_added, auto=auto_kind, words=total_words + sum(words(m) for m in meta), headings=0, blockquotes=0, split=0, derived_title=derived and not doc["title"], titled=bool(title))

    if first and pre_words >= 40:
        pre_h = "Opening Prayer" if (first in markers and markers[first][1].startswith("Prayer")) else "Introduction"
        out += [f"## {pre_h} {{.unnumbered}}", ""]
        stats["headings"] += 1
    last_h2 = None
    for idx, raw in enumerate(body):
        if idx in consumed:
            continue
        for lv, tx in inserts.get(idx, []):
            lv = max(2, lv - shift)
            out += [f"{'#' * lv} {plain(tx)} {{.unnumbered}}", ""]
            stats["headings"] += 1
            if lv == 2:
                last_h2 = plain(tx)
        if idx in markers:
            lv, tx, _, unity = markers[idx]
            lv = max(2, lv - shift)
            if unity and last_h2 != unity:
                last_h2 = unity
                out += [f"## {last_h2} {{.unnumbered}}", ""]
                stats["headings"] += 1
            out += [f"{'#' * lv} {plain(tx)} {{.unnumbered}}", ""]
            stats["headings"] += 1
            if lv == 2:
                last_h2 = plain(tx)
            if idx in notes:
                out += [f"*{plain(notes[idx])}*", ""]
            continue
        p = clean_text(raw)
        if re.match(r"^\[[^\[\]]+\]$", raw.strip()):          # whole-line editorial note
            out += [f"*{plain(raw.strip())}*", ""]
            continue
        if is_whole_quote(raw.strip()):
            out += ["> " + p, ""]
            stats["blockquotes"] += 1
            continue
        emb = split_embedded_quote(raw)
        if emb:
            lead, quote, rest = emb
            for c in split_long_paragraph(clean_text(lead)):
                out += [c, ""]
            out += ["> " + clean_text(quote), ""]
            stats["blockquotes"] += 1
            if rest:
                for c in split_long_paragraph(clean_text(rest)):
                    out += [c, ""]
            continue
        chunks = split_long_paragraph(p)
        if len(chunks) > 1:
            stats["split"] += 1
        for c in chunks:
            out += [c, ""]
    return "\n".join(out).rstrip() + "\n", stats


# ---------------------------------------------------------------- parsing
def parse_source(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks, cur = [], []
    for line in text.splitlines():
        if SEP_RE.match(line):
            blocks.append(cur)
            cur = []
        else:
            cur.append(line)
    blocks.append(cur)
    docs = []
    for b in blocks:
        b = [l.strip() for l in b if l.strip()]
        if not b:
            continue
        m = HEAD_RE.match(b[0])
        if not m:
            docs.append(dict(id=None, title="", lines=b, bad=True))
        else:
            docs.append(dict(id=m.group(1), title=m.group(2).strip(), lines=b[1:], bad=False))
    return docs


def volume_info(path):
    stem = path.name
    for suf in (".txt.md", ".md", ".txt"):
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    m = re.match(r"^(\d+(?:-\d+)*)[\s_]+(.*)$", stem)
    prefix, title = (m.group(1), m.group(2)) if m else ("", stem)
    if "_" in title and " " not in title:      # sanitized upload name: restore parentheses/commas
        title = re.sub(r"__(.+?)_+$", r" (\1)", title)
        title = title.replace("__", ", ").replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip()
    years = [int(y) for y in re.findall(r"(?<!\d)(18[3-5]\d)(?!\d)", title)]
    return prefix, title, years


def part_for(years):
    if not years:
        return UNDATED, "no year in file name"
    start, end = min(years), max(years)
    for p in PARTS:
        if p[2] <= start <= p[3]:
            note = None if p[2] <= end <= p[3] else f"spans parts (years {start}–{end}); placed by start year"
            return p, note
    return UNDATED, f"year {start} outside 1844–1850"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="source")
    ap.add_argument("--out", default=".")
    ap.add_argument("--book-title", default=None, help="default: read from _quarto.yml")
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)
    if not args.book_title:
        args.book_title = "A Compilation of the Bab's Writings"
        try:
            import yaml
            args.book_title = yaml.safe_load((out / "_quarto.yml").read_text(encoding="utf-8"))["book"]["title"]
        except Exception:
            pass
    files = sorted({p for p in src.iterdir() if p.is_file() and (p.suffix in (".md", ".txt", ".qmd"))}, key=lambda p: p.name)
    if not files:
        sys.exit(f"No source files in {src}")

    titles = load_json(out / "tools" / "titles.json")
    headings = load_json(out / "tools" / "headings.json")
    options = load_json(out / "tools" / "options.json")
    strip_fn = set(options.get("strip_footnotes", []))

    chap_root = out / "chapters"
    if chap_root.exists():
        shutil.rmtree(chap_root)
    chap_root.mkdir(parents=True)

    review = {k: [] for k in ("undated", "spans", "untitled", "derived", "long_nohead", "mojibake", "bad_blocks", "dup_ids", "placeholder", "footnotes", "auto")}
    seen, total_kept, total_dropped = {}, 0, 0
    units = {p[0]: [] for p in PARTS + [UNDATED]}      # per part: list of volumes [(files, words, name)]
    part_words = {p[0]: 0 for p in PARTS + [UNDATED]}
    vol_summary = []

    for f in files:
        prefix, vtitle, years = volume_info(f)
        part, note = part_for(years)
        if note:
            (review["undated"] if not years else review["spans"]).append(f"{f.name} — {note}")
        pkey = part[0]
        pdir = chap_root / pkey
        pdir.mkdir(exist_ok=True)
        kept = []
        for d in parse_source(f):
            if d.get("bad"):
                review["bad_blocks"].append(f"{f.name}: block starting “{d['lines'][0][:60]}”")
            elif not KEEP_ID_RE.match(d["id"]):
                total_dropped += 1
            elif sum(words(l) for l in d["lines"]) <= 12 and any(PLACEHOLDER_RE.search(l) for l in d["lines"]):
                review["placeholder"].append(f"{d['id']} {d['title']} ({prefix}) — source only says “{' '.join(d['lines'])[:60]}”")
            else:
                kept.append(d)
        if not kept:
            continue

        vol_file = pdir / f"vol-{slug(prefix) or slug(vtitle)[:30]}.qmd"
        vol_file.write_text(f"# {plain(vtitle)} {{.unnumbered}}\n\n*Volume {prefix}. Contains {len(kept)} writing{'s' if len(kept) != 1 else ''}.*\n", encoding="utf-8")
        entries, vwords = [str(vol_file.relative_to(out))], 0
        for d in kept:
            if d["id"] in seen:
                review["dup_ids"].append(f"{d['id']} in {f.name} and {seen[d['id']]}")
            seen[d["id"]] = f.name
            if d["id"] in strip_fn:
                d["lines"] = [strip_footnotes(l) for l in d["lines"]]
            qmd, st = build_doc_qmd(d, titles.get(d["id"]), headings.get(d["id"]))
            if "^[" in qmd and d["id"] not in strip_fn:
                review["footnotes"].append(f"{d['id']} ({prefix}) — {qmd.count('^[')} footnotes kept; to remove, add the ID to \"strip_footnotes\" in tools/options.json")
            if not st["titled"]:
                review["untitled"].append(f"{d['id']} ({prefix}) — first words: “{' '.join(d['lines'][0].split()[:9]) if d['lines'] else ''}…”")
            elif st["derived_title"]:
                review["derived"].append(f"{d['id']} ({prefix}) — title taken from the text: “{qmd.splitlines()[0][2:].split(' {')[0]}”")
            if re.search(r"[ÃÂ�]", qmd):
                review["mojibake"].append(f"{d['id']} ({prefix})")
            if st.get("auto"):
                review["auto"].append(f"{d['id']} ({prefix}) {d['title']} — {st['headings']} headings by {st['auto']} ({st['words']:,} words)")
            if st["words"] > LONG_DOC_WORDS and st["headings"] == 0:
                review["long_nohead"].append(f"{d['id']} ({prefix}) {d['title']} — {st['words']:,} words")
            (pdir / f"{d['id'].lower()}.qmd").write_text(qmd, encoding="utf-8")
            entries.append(str((pdir / f"{d['id'].lower()}.qmd").relative_to(out)))
            vwords += st["words"]
            total_kept += 1
        units[pkey].append((entries, vwords, prefix))
        part_words[pkey] += vwords
        vol_summary.append((f.name, pkey, len(kept), vwords))

    # ---- editions: group whole source volumes, in order, under the word limit
    editions = []     # (profile, title, [files], words)
    for p in PARTS + [UNDATED]:
        if not units[p[0]]:
            continue
        total = sum(u[1] for u in units[p[0]])
        n0 = -(-total // EDITION_WORD_LIMIT)
        for n_try in range(n0, n0 + 6):          # fewest, most evenly sized editions that fit
            cap = min(EDITION_WORD_LIMIT, int(total / n_try * 1.12))
            groups, cur, cw = [], [], 0
            for entries, w, prefix in units[p[0]]:
                if cur and cw + w > cap:
                    groups.append((cur, cw))
                    cur, cw = [], 0
                cur += entries
                cw += w
            if cur:
                groups.append((cur, cw))
            if len(groups) <= n_try and all(g[1] <= EDITION_WORD_LIMIT for g in groups):
                break
        for n, (ents, w) in enumerate(groups, 1):
            label = p[4].replace("-", "–") + (f", Volume {n}" if len(groups) > 1 else "")
            prof = f"docx-{p[0]}-v{n}"
            fname = "Bab-Compilation-GPT_" + p[4] + (f"_Vol-{n}" if len(groups) > 1 else "")
            editions.append((prof, f"{args.book_title} — {label}", ents, w, p[1], fname, label))

    # ---- yaml
    def write_once(path, text):
        """Files you own: created only if missing, so your edits are never overwritten."""
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            return True
        return False

    write_once(out / "_quarto.yml", f"""# YOUR settings file. The build script creates it once and never overwrites it.
# Chapter lists are generated into _quarto-web.yml and _quarto-docx-*.yml (profiles).
project:
  type: book

profile:
  default: web

book:
  title: {q(args.book_title)}
  subtitle: "Translated Using GPT-4"
  author: "The Bab"
  language: en
  site-url: https://bab.peopleofbaha.world
  reader-mode: true
  search: true
  page-footer:
    left: |
      Created by the Baha'i Translation Project
    right: |
      Translations used were created with GPT 4

execute:
  eval: false

# Render each format explicitly (render_all.py does this): --to html / --to docx
format:
  html:
    theme:
      - minty
      - assets/custom.scss
    fontsize: 10pt
    toc: true
    toc-depth: 6
    number-sections: false
    smooth-scroll: true
  docx:
    reference-doc: reference1.docx
    toc: true
    toc-depth: 1
    number-sections: false

# No PDF format on purpose: convert the DOCX files to PDF in Word.
""")

    web = ["# GENERATED by tools/build_book.py - do not edit (edit _quarto.yml instead)",
           "project:", "  output-dir: _book", "", "book:", "  chapters:", "    - index.qmd", "    - downloads.qmd"]
    for p in PARTS + [UNDATED]:
        if units[p[0]]:
            web += [f"    - part: {q(p[1])}", "      chapters:"]
            for entries, _, _ in units[p[0]]:
                web += [f"        - {e}" for e in entries]
    (out / "_quarto-web.yml").write_text("\n".join(web) + "\n", encoding="utf-8")

    dl = ["# Downloads {.unnumbered}", "",
          "Word (.docx) editions of this compilation, grouped by period. Each edition is a separate file.", ""]
    dl += [f"- [{e[6]}](downloads/{e[5]}.docx) — about {round(e[3], -3):,} words" for e in editions]
    (out / "downloads.qmd").write_text("\n".join(dl) + "\n", encoding="utf-8")

    for old in out.glob("_quarto-docx-*.yml"):
        old.unlink()
    for prof, etitle, ents, w, plabel, fname, elabel in editions:
        y = ["# GENERATED by tools/build_book.py - do not edit (edit _quarto.yml instead)",
             "project:", f"  output-dir: _docx/{prof[5:]}", "", "book:", f"  title: {q(etitle)}",
             f"  output-file: {q(fname)}", "  chapters:", "    - index.qmd"]
        y += [f"    - {e}" for e in ents]
        (out / f"_quarto-{prof}.yml").write_text("\n".join(y) + "\n", encoding="utf-8")

    old_sh = out / "tools" / "render_all.sh"
    if old_sh.exists():
        old_sh.unlink()
    write_once(out / "index.qmd",
        "# Preface {.unnumbered}\n\nThis book collects the writings of the Báb in chronological order, arranged in three parts: "
        "1844–1846, 1847 (the year of the Bayán), and 1848–1850.\n\nEach writing is identified by its BB reference number.\n")
    (out / "assets").mkdir(exist_ok=True)
    write_once(out / "assets" / "custom.scss",
        "/*-- scss:rules --*/\nblockquote { border-left: 4px solid #b9a37a; padding-left: 1rem; font-style: italic; }\nbody { line-height: 1.7; }\n")
    write_once(out / "netlify.toml",
        "# The site is rendered on your computer (tools/render_all.py) and the _book folder is committed.\n"
        "# Netlify only publishes it: leave the build command EMPTY in the Netlify dashboard.\n"
        '[build]\n  publish = "_book"\n')
    write_once(out / "_quarto-nosearch.yml", "# Used by: python tools/render_all.py --no-search\nbook:\n  search: false\n")
    write_once(out / ".gitignore", "/.quarto/\n/_docx/\n__pycache__/\n")

    # ---- review report
    rep = ["# Review report", "", f"Writings kept: **{total_kept}**; BBU (or other non-BB) dropped: **{total_dropped}**.", "",
           f"## DOCX editions (each under {EDITION_WORD_LIMIT:,} words; Gemini Notebook limit is 500,000 per source)", "",
           "| Profile | Part | Words |", "|---|---|---:|"]
    rep += [f"| {e[0]} | {e[4]} | {e[3]:,} |" for e in editions]
    rep += ["", "## Word counts by part", "", "| Part | Words |", "|---|---:|"]
    rep += [f"| {p[1]} | {part_words[p[0]]:,} |" for p in PARTS + [UNDATED] if part_words[p[0]]]
    rep += [f"| **Total** | **{sum(part_words.values()):,}** |", "", "## Volumes", "", "| File | Part | Writings | Words |", "|---|---|---:|---:|"]
    rep += [f"| {n} | {k} | {c} | {w:,} |" for n, k, c, w in vol_summary]
    labels = dict(
        undated="Files with no year in the name (placed in 'Undated')",
        spans="Files whose years span two parts (placed by start year)",
        untitled="Writings with no title (shown as the BB number only) — add names to tools/titles.json",
        derived="Titles taken from the text itself (check these)",
        long_nohead=f"Long writings (> {LONG_DOC_WORDS:,} words) with NO sub-headers — add to tools/headings.json",
        mojibake="Writings that still contain garbled characters (Ã, Â, �)",
        bad_blocks="Blocks that did not begin with a BB ID",
        dup_ids="Duplicate IDs",
        auto="Long writings given AUTOMATIC headings (verse / question / incipit) — override any in tools/headings.json",
        footnotes="Writings containing footnotes (BB00151's footnotes are labeled as an experimental AI-generated gloss in the source)",
        placeholder="Excluded: writings with no text in the source (placeholder only)")
    for k, lab in labels.items():
        rep += ["", f"## {lab}", ""] + ([f"- {x}" for x in review[k]] or ["- none"])
    (out / "tools" / "review_report.md").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep[:rep.index("## Volumes")]))
    print({k: len(v) for k, v in review.items()})


if __name__ == "__main__":
    main()
