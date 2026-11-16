"""Builds the trophy shelf SVGs (dark and light) from my PRs and my awards file.

Run it from the repo root:  python badges/render.py
It needs a GitHub token in GITHUB_TOKEN (the Action has one). For a test without
the network:  python badges/render.py --prs saved-prs.json

All text is turned into shapes here, because a README shows SVGs as images and
images can't load fonts.
"""

import json
import re
import os
import random
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
CONFIG = tomllib.loads((HERE / "config.toml").read_text(encoding="utf-8"))
AWARDS = tomllib.loads((HERE / "achievements.toml").read_text(encoding="utf-8")).get("award", [])
STATE_FILE = HERE / "state.json"

# ---------- getting the data ----------

def fetch_prs():
    """Every PR I opened in someone else's repo, newest first."""
    token = os.environ.get("GITHUB_TOKEN", "")
    user = CONFIG["user"]
    query = f"author:{user} type:pr -user:{user} " + " ".join(f"-org:{o}" for o in CONFIG.get("skip_orgs", []))
    items, page = [], 1
    while True:
        url = "https://api.github.com/search/issues?" + urllib.parse.urlencode(
            {"q": query, "per_page": 100, "page": page, "sort": "created", "order": "desc"})
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "billibuster-badges"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        items += data["items"]
        if len(items) >= data["total_count"] or not data["items"]:
            break
        page += 1
    return [simplify(i) for i in items]


def simplify(item):
    repo = item["repository_url"].split("/repos/", 1)[1]
    return {
        "repo": repo,
        "number": item["number"],
        "title": item["title"],
        "state": item["state"],
        "created_at": item["created_at"],
        "merged_at": (item.get("pull_request") or {}).get("merged_at"),
    }


def when(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

# ---------- counting the badges ----------

def count_badges(prs, state):
    skip = set(CONFIG.get("skip_repos", []))
    prs = [p for p in prs if p["repo"] not in skip]
    merged = [p for p in prs if p["merged_at"]]
    open_now = [p for p in prs if p["state"] == "open"]

    per_repo = {}
    for p in merged:
        per_repo[p["repo"]] = per_repo.get(p["repo"], 0) + 1

    counts = {
        "1ST": len(per_repo),                                   # first merge in a new repo
        "NORMIE": sum(1 for n in per_repo.values() if n >= 3),  # 3 merges in one repo
        "ZOOM": sum(1 for p in merged if (when(p["merged_at"]) - when(p["created_at"])).total_seconds() <= 24 * 3600),
    }
    # CLEAR goes up each time the open list goes from some to none.
    clear = state.get("clear", 0)
    if not open_now and state.get("open_last_run", 1) > 0:
        clear += 1
    counts["CLEAR"] = clear

    for a in AWARDS:  # the ones only I can judge
        counts[a["badge"]] = counts.get(a["badge"], 0) + 1

    new_state = {"clear": clear, "open_last_run": len(open_now)}
    return counts, merged, prs, new_state


def level_for(merges):
    steps = CONFIG["levels"]
    level = sum(1 for s in steps if merges >= s)
    nxt = next((s for s in steps if s > merges), None)
    return level, nxt


def latest_events(prs):
    names = CONFIG.get("names", {})
    ev = []
    for p in prs:
        name = names.get(p["repo"], p["repo"].split("/")[1])
        title = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", p["title"])  # drop "fix(core): " style prefixes
        text = f'{name} #{p["number"]}: {title[:1].upper() + title[1:]}'
        if p["merged_at"]:
            ev.append((when(p["merged_at"]), "QUEST CLEARED", "green", text))
        elif p["state"] == "open":
            ev.append((when(p["created_at"]), "QUEST STARTED", "orange", text))
    for a in AWARDS:
        if a.get("date"):
            ev.append((datetime.fromisoformat(a["date"] + "T12:00:00+00:00"), "ACHIEVEMENT", "lavender", f'{a["badge"]}. {a.get("note", "")}'))
    ev.sort(key=lambda e: e[0], reverse=True)
    return ev[: CONFIG.get("latest", 3)]

# ---------- text as shapes ----------

class Face:
    def __init__(self, key, file):
        self.key = key
        self.font = TTFont(HERE / "fonts" / file)
        self.glyphs = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.upm = self.font["head"].unitsPerEm
        self.hmtx = self.font["hmtx"]
        self.used = {}

    def gname(self, ch):
        return self.cmap.get(ord(ch)) or self.cmap.get(ord("?"))

    def width(self, text, size, ls=0.0):
        return sum(self.hmtx[self.gname(c)][0] for c in text) * size / self.upm + ls * size * max(len(text) - 1, 0)

    def defs(self):
        out = []
        for g, d in self.used.items():
            out.append(f'<path id="{self.key}-{g}" d="{d}"/>')
        return "".join(out)

    def draw(self, text, x, y, size, fill, ls=0.0, anchor="start"):
        """y is the baseline."""
        w = self.width(text, size, ls)
        if anchor == "middle":
            x -= w / 2
        elif anchor == "end":
            x -= w
        s = size / self.upm
        parts = []
        for c in text:
            g = self.gname(c)
            if g not in self.used:
                pen = SVGPathPen(self.glyphs)
                self.glyphs[g].draw(pen)
                self.used[g] = pen.getCommands()
            if self.used[g]:
                parts.append(f'<use href="#{self.key}-{g}" transform="translate({x:.1f} {y:.1f}) scale({s:.4f} {-s:.4f})"/>')
            x += self.hmtx[g][0] * s + ls * size
        return f'<g fill="{fill}">{"".join(parts)}</g>'

    def fit(self, text, size, max_w, ls=0.0):
        """Cut text down with an ellipsis until it fits."""
        if self.width(text, size, ls) <= max_w:
            return text
        while text and self.width(text + "…", size, ls) > max_w:
            text = text[:-1]
        return text.rstrip() + "…"

    def wrap(self, text, size, max_w):
        lines, line = [], ""
        for word in text.split():
            test = (line + " " + word).strip()
            if self.width(test, size) <= max_w or not line:
                line = test
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines


DIRT = Face("d", "RubikDirt-Regular.ttf")
BUNGEE = Face("b", "Bungee-Regular.ttf")
VT = Face("v", "VT323-Regular.ttf")
MONO = Face("m", "SpaceMono-Regular.ttf")
MONOB = Face("mb", "SpaceMono-Bold.ttf")

# ---------- the picture ----------

THEMES = {
    "dark": {
        "bg": "#140E1A", "card": "#22192B", "text": "#F7F1FB", "muted": "#9C8FA8", "soft": "#C9BBD6", "faint": "#6E6278",
        "lockline": "#4A3560", "locktext": "#7E728A", "chiptext": "#140E1A", "glow": 0.45,
        "gold": "#FFB547", "green": "#7CFFB2", "lavender": "#E8B4FF", "orange": "#FF6700",
    },
    "light": {
        "bg": "#F7F1FB", "card": "#FFFFFF", "text": "#22192B", "muted": "#6E6278", "soft": "#4A3560", "faint": "#8A7E96",
        "lockline": "#A99BB8", "locktext": "#9C8FA8", "chiptext": "#F7F1FB", "glow": 0.22,
        "gold": "#B86E00", "green": "#138A55", "lavender": "#8A4FBF", "orange": "#E05A00",
    },
}

W, PAD = 812, 40


def hex_points(r, cx, cy):
    import math
    pts = []
    for k in range(6):
        a = math.radians(-90 + 60 * k)
        pts.append(f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}")
    return " ".join(pts)


GLINTS = []


def badge(t, x, y, word, count, colour, earned):
    """A 132 x 132 badge with its top-left at x, y."""
    c = t[colour] if earned else t["lockline"]
    cx, cy = x + 66, y + 66
    ticks = "".join(f"M{x + 14} {cy + d} H{x + 8} M{x + 118} {cy + d} H{x + 124} " for d in (-14, 0, 14))
    star = (f"M{cx} {y + 6} L{cx + 2} {y + 11} L{cx + 7} {y + 11} L{cx + 3} {y + 14} L{cx + 4} {y + 19} "
            f"L{cx} {y + 16} L{cx - 4} {y + 19} L{cx - 3} {y + 14} L{cx - 7} {y + 11} L{cx - 2} {y + 11} Z")
    glow = f' filter="url(#glow-{colour})"' if earned else ' opacity="0.75"'
    out = [f'<g{glow}>',
           f'<polygon points="{hex_points(56, cx, cy)}" fill="{t["card"]}" stroke="{c}" stroke-width="4"/>',
           f'<polygon points="{hex_points(47, cx, cy)}" fill="none" stroke="{c}" stroke-width="3" stroke-dasharray="2 4" opacity="0.8"/>',
           f'<path d="{star}" fill="{c}"/>',
           f'<path d="{ticks}" stroke="{c}" stroke-width="3" stroke-linecap="round"/>',
           "</g>"]
    if earned:
        # Each earned badge smoulders on its own: a warm glow breathing at its base and a few embers of its own
        # colour rising through it. The timings come from the badge's word, so they differ but never change.
        rnd = random.Random(word)
        GLINTS.append(colour)
        n = len(GLINTS)
        parts = [f'<clipPath id="hex-{n}"><polygon points="{hex_points(56, cx, cy)}"/></clipPath><g clip-path="url(#hex-{n})">',
                 f'<ellipse class="smoulder" style="--d:{rnd.uniform(3.2, 4.8):.1f}s;animation-delay:-{rnd.uniform(0, 4):.1f}s" '
                 f'cx="{cx}" cy="{cy + 52}" rx="52" ry="30" fill="url(#ember-{colour})"/>']
        for _ in range(5):
            d = rnd.uniform(3.0, 5.5)
            parts.append(f'<circle class="ember" style="--d:{d:.1f}s;--dx:{rnd.uniform(-10, 10):.0f}px;animation-delay:-{rnd.uniform(0, d):.1f}s" '
                         f'cx="{cx + rnd.uniform(-30, 30):.0f}" cy="{cy + 46}" r="{rnd.uniform(1.2, 2.4):.1f}" fill="{t[colour]}"/>')
        out.append("".join(parts) + "</g>")
    text = word if earned else "???"
    size = 30
    w = DIRT.width(text, size)
    if w > 80:
        size = size * 80 / w
    out.append(DIRT.draw(text, cx, cy + size * 0.36, size, t[colour] if earned else t["locktext"], anchor="middle"))
    if earned:
        label = f"×{count}"
        cw = BUNGEE.width(label, 16) + 18
        out.append(f'<rect x="{x + 132 - cw:.1f}" y="{y + 132 - 26}" width="{cw:.1f}" height="26" rx="12" fill="{t[colour]}" stroke="{t["bg"]}" stroke-width="3"/>')
        out.append(BUNGEE.draw(label, x + 132 - cw / 2, y + 132 - 7, 16, t["chiptext"], anchor="middle"))
    return "".join(out)


def render(theme, counts, merges, events):
    t = THEMES[theme]
    body, y = [], PAD
    GLINTS.clear()

    y += 18
    body.append(VT.draw("> CONNECTING TO OPEN SOURCE... OK", W / 2, y, 22, t["green"], ls=0.04, anchor="middle"))
    # The title fades in line by line, then a warm band sweeps across the letters every few seconds.
    # "BILLIAMBASH PRESENTS:" is wider than the card at full size, so that line shrinks to fit.
    top = y + 8
    lines = []
    y += 58
    lines.append(("BILLIAMBASH PRESENTS:", y, min(62, 62 * (W - 2 * PAD) / DIRT.width("BILLIAMBASH PRESENTS:", 62))))
    y += 38
    lines.append(("THE", y, 26))
    for word in ("BILLIBASHING", "BILLIBUSTER"):
        y += 62
        lines.append((word, y, 62))
    glyphs = []
    for i, (text, ly, size) in enumerate(lines):
        g = DIRT.draw(text, W / 2, ly, size, t["text"], anchor="middle")
        body.append(g.replace("<g ", f'<g class="t" style="animation-delay:{0.2 + 0.25 * i:.2f}s" ', 1))
        glyphs += re.findall(r"<use [^>]*/>", g)
    body.append(f'<g clip-path="url(#title)"><rect class="band" x="0" y="{top:.0f}" width="160" height="{y + 14 - top:.0f}" fill="url(#band)"/></g>')
    y += 34
    body.append(MONO.draw(CONFIG["tagline"], W / 2, y, 15, t["muted"], anchor="middle"))

    level, nxt = level_for(merges)
    y += 44
    xp = f"LEVEL {level} CONTRIBUTOR · {merges} MERGE{'S' if merges != 1 else ''}"
    if nxt:
        xp += f" · {nxt - merges} MORE TO LEVEL {level + 1}"
    body.append(VT.draw(xp, PAD, y, 22, t["soft"], ls=0.04))
    y += 12
    frac = merges / nxt if nxt else 1
    body.append(f'<rect x="{PAD}" y="{y}" width="{W - 2 * PAD}" height="12" rx="6" fill="{t["card"]}"/>')
    body.append(f'<rect x="{PAD}" y="{y}" width="{max((W - 2 * PAD) * frac, 12):.1f}" height="12" rx="6" fill="{t["orange"]}" filter="url(#glow-orange)"/>')

    shelf = CONFIG["badge"]
    unlocked = sum(1 for b in shelf if counts.get(b["word"], 0) > 0)
    y += 52
    body.append(BUNGEE.draw(f"TROPHY SHELF · {unlocked} OF {len(shelf)} UNLOCKED", PAD, y, 20, t["orange"], ls=0.04))
    y += 24
    cell_w = (W - 2 * PAD - 3 * 14) / 4
    for row in range(0, len(shelf), 4):
        row_h = 0
        for i, b in enumerate(shelf[row:row + 4]):
            n = counts.get(b["word"], 0)
            earned = n > 0
            x0 = PAD + i * (cell_w + 14)
            cx = x0 + cell_w / 2
            body.append(badge(t, cx - 66, y, b["word"], n, b["colour"], earned))
            ty = y + 132 + 26
            name_col = t[b["colour"]] if earned else t["locktext"]
            for line in VT.wrap(b["name"].upper(), 20, 150):
                body.append(VT.draw(line, cx, ty, 20, name_col, ls=0.04, anchor="middle"))
                ty += 20
            ty += 8
            for line in MONO.wrap(b["rule"], 12, 150):
                body.append(MONO.draw(line, cx, ty, 12, t["muted"] if earned else t["faint"], anchor="middle"))
                ty += 18
            row_h = max(row_h, ty - y)
        y += row_h + 20

    y += 14
    body.append(BUNGEE.draw("LATEST", PAD, y, 20, t["orange"], ls=0.04))
    y += 18
    for when_, label, colour, text in events:
        body.append(f'<rect x="{PAD}" y="{y}" width="{W - 2 * PAD}" height="50" rx="4" fill="{t["card"]}"/>')
        body.append(f'<rect x="{PAD + 14}" y="{y + 10}" width="4" height="30" fill="{t[colour]}"/>')
        x = PAD + 32
        date = when_.strftime("%m-%d")
        body.append(VT.draw(date, x, y + 31, 20, t["faint"]))
        x += VT.width(date, 20) + 14
        body.append(VT.draw(label, x, y + 31, 20, t[colour], ls=0.06))
        x += VT.width(label, 20, 0.06) + 14
        body.append(MONO.draw(MONO.fit(text, 14, W - PAD - 16 - x), x, y + 30, 14, t["text"]))
        y += 60

    h = y + PAD - 10
    defs = "".join(
        f'<filter id="glow-{k}" x="-30%" y="-30%" width="160%" height="160%">'
        f'<feDropShadow dx="0" dy="0" stdDeviation="8" flood-color="{t[k]}" flood-opacity="{t["glow"]}"/></filter>'
        for k in ("gold", "green", "lavender", "orange"))
    defs += "".join(f.defs() for f in (DIRT, BUNGEE, VT, MONO, MONOB))
    defs += "".join(
        f'<radialGradient id="ember-{k}"><stop offset="0" stop-color="{t[k]}" stop-opacity=".55"/>'
        f'<stop offset="1" stop-color="{t[k]}" stop-opacity="0"/></radialGradient>'
        for k in sorted(set(GLINTS)))
    defs += (f'<linearGradient id="band" x1="0" x2="1"><stop offset="0" stop-color="{t["orange"]}" stop-opacity="0"/>'
             f'<stop offset=".5" stop-color="{t["orange"]}" stop-opacity=".75"/><stop offset="1" stop-color="{t["orange"]}" stop-opacity="0"/></linearGradient>'
             f'<clipPath id="title">{"".join(glyphs)}</clipPath>')
    # People who ask their system for less motion get a still shelf: no sweep, no embers, a steady glow.
    style = ("<style>.t{animation:fade 1s ease both}"
             ".band{transform:translateX(-300px);animation:sweep 7s ease-in-out infinite 2.4s}"
             ".smoulder{animation:breathe var(--d) ease-in-out infinite alternate}"
             ".ember{opacity:0;animation:rise var(--d) linear infinite}"
             "@keyframes fade{from{opacity:0}to{opacity:1}}"
             "@keyframes sweep{0%{transform:translateX(-300px)}40%,100%{transform:translateX(1100px)}}"
             "@keyframes breathe{from{opacity:.25}to{opacity:.75}}"
             "@keyframes rise{0%{transform:translate(0,0);opacity:0}15%{opacity:.9}100%{transform:translate(var(--dx),-64px);opacity:0}}"
             "@media (prefers-reduced-motion: reduce){*{animation:none!important}.band,.ember{display:none}.smoulder{opacity:.4}}</style>")
    title = "BilliBaSHing BilliBuster trophy shelf"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h:.0f}" viewBox="0 0 {W} {h:.0f}" role="img" aria-label="{title}">'
            f"<title>{title}: {unlocked} of {len(shelf)} badges, level {level}</title>{style}<defs>{defs}</defs>"
            f'<rect width="{W}" height="{h:.0f}" rx="12" fill="{t["bg"]}"/>{"".join(body)}</svg>\n')


def main():
    if "--prs" in sys.argv:
        prs = json.loads(Path(sys.argv[sys.argv.index("--prs") + 1]).read_text(encoding="utf-8"))
    else:
        prs = fetch_prs()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    counts, merged, kept, new_state = count_badges(prs, state)
    events = latest_events(kept)
    for theme in THEMES:
        for f in (DIRT, BUNGEE, VT, MONO, MONOB):
            f.used = {}
        (HERE / f"shelf-{theme}.svg").write_text(render(theme, counts, len(merged), events), encoding="utf-8", newline="\n")
    STATE_FILE.write_text(json.dumps(new_state, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("badges:", {k: v for k, v in counts.items() if v}, "| merges:", len(merged), "| open:", new_state["open_last_run"])


if __name__ == "__main__":
    main()
