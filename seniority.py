"""
seniority.py – the organization's seniority, read from job titles by rules
the user keeps, and its health in numbers read from the tree.

Titles are free text, and in a real directory a chaotic one: the same role
with a country code behind it ("Engineer III - NL", "Engineer III – AT"), a
grade ladder in roman numerals, "Manager" for a role that leads nobody,
contractors and mailboxes beside the people. Everything here is
deterministic – the same version and the same role model give the same
pyramid –, and nothing guesses: a title no rule covers stays "not
assigned" until the user places it.

How a title gets its level, for one person:

1. The title is cleaned (`normalize`): a trailing country or region code
   (not a grade, not a rule's own word – "Co-CEO") and a trailing remark
   in brackets go, dashes become one kind, spaces
   one each; the words that never decide a level (`never`) go too. The
   cleaned title is the group the review dialog lists.
2. A level set by hand for the group wins (`overrides`) – over
   everything below, the outside words too.
3. A title naming someone outside the organization (`outside`: a
   contractor, a shared mailbox) is counted apart, not in the pyramid.
4. Else every rule found in the title competes – as whole words, without
   case: the rule standing EARLIEST in the title wins, since a title puts
   its rank first ("Senior Engineer" and "Senior Project Manager" are
   senior, "Junior Consultant" junior, "Account Manager" before
   "Manager"); of rules starting at the same word the one of MORE words
   ("Lead Architect" before "Lead", "Senior Manager" before "Senior");
   then the higher level. Starting earliest beats more words: with more
   words first, a two-word professional rule ("Project Manager") would
   outrank the one-word "Senior" in front of it.
5. A leadership level (`needs_reports`) holds only for someone who leads
   somebody in the tree: a "Manager" without reports is a professional
   (`no_reports_level`). The tree decides, not the word.
6. A grade after the role ("Engineer III") moves the plain professional
   level (`grade_level`) along the ladder (`grades`).

The health figures come from the tree alone: who leads anybody (direct
active reports), how many each leads (span of control), how many people
there are per leader, how many layers the longest line has, and where
title and tree disagree. They are numbers; no title or name leaves this
module but in `titles()`, which only the page's review dialog asks.
"""

import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict

LEVELS = 10

# The default role model ("corporate"): ten levels from the top down, each
# with its rule words, comma-separated.
CORPORATE = [
    ("Executive board", "Chief, CEO, CFO, CTO, COO, CIO, CHRO, CISO, EVP, Executive Vice President, "
                        "Managing Director, Geschäftsführer, Geschäftsführerin, Vorstand"),
    ("Senior leadership", "Vice President, VP, SVP, Senior Vice President, Senior Director, Director, "
                          "Executive Director, General Manager, GM, Bereichsleiter, Bereichsleiterin"),
    ("Head of", "Head of, Abteilungsleiter, Abteilungsleiterin, Leiter, Leiterin, Leader"),
    ("Senior manager", "Senior Manager, Associate Director"),
    ("Manager, team lead", "Manager, Team Lead, Teamleiter, Teamleiterin, Supervisor, "
                           "Associate Manager"),
    ("Lead expert, principal", "Principal, Staff, Distinguished, Fellow, Lead, Lead Engineer, "
                               "Lead Architect, Chief Architect, Chief Engineer"),
    ("Senior professional", "Senior, Sr, Sr., Architect, Partner"),
    ("Professional", "Engineer, Consultant, Specialist, Analyst, Developer, Administrator, Admin, "
                     "Technician, Accountant, Recruiter, Trainer, Auditor, Coordinator, "
                     "Representative, Designer, Writer, Scientist, Researcher, Operator, Planner, "
                     "Buyer, Officer, Advisor, Executive, Executive Assistant, Account Manager, "
                     "Project Manager, Program Manager, Product Manager, Customer Success Manager, "
                     "Referent, Referentin, Berater, Beraterin, Sachbearbeiter, Sachbearbeiterin"),
    ("Junior, associate", "Junior, Associate, Assistant, Assistenz"),
    ("Trainee, intern, working student", "Trainee, Intern, Praktikant, Praktikantin, Werkstudent, "
                                         "Werkstudentin, Auszubildende, Auszubildender, Azubi"),
]

PRESETS = {
    "corporate": CORPORATE,
    "consulting": [
        ("Partner", "Managing Partner, Senior Partner, Partner, Managing Director"),
        ("Principal, director", "Principal, Director, Associate Partner"),
        ("Senior manager", "Senior Manager, Associate Director"),
        ("Manager", "Manager, Engagement Manager, Project Lead"),
        ("Senior consultant", "Senior Consultant, Senior Associate"),
        ("Consultant", "Consultant, Associate, Specialist"),
        ("Analyst", "Analyst, Business Analyst"),
        ("Junior analyst", "Junior, Junior Analyst"),
        ("Assistant", "Assistant, Office, Coordinator"),
        ("Intern, working student", "Intern, Trainee, Praktikant, Werkstudent"),
    ],
    "public": [
        ("Head of authority", "Präsident, Präsidentin, Direktor, Direktorin, Leitung der Behörde"),
        ("Department head", "Abteilungsleiter, Abteilungsleiterin, Abteilungsleitung"),
        ("Division head", "Referatsleiter, Referatsleiterin, Unterabteilungsleiter, Gruppenleiter"),
        ("Section head", "Sachgebietsleiter, Sachgebietsleiterin, Teamleiter, Teamleiterin"),
        ("Senior officer", "Oberrat, Oberrätin, Regierungsdirektor, Regierungsdirektorin"),
        ("Officer", "Referent, Referentin, Rat, Rätin"),
        ("Clerk, senior", "Oberinspektor, Amtmann, Amtfrau, Hauptsachbearbeiter"),
        ("Clerk", "Sachbearbeiter, Sachbearbeiterin, Inspektor, Inspektorin"),
        ("Assistant", "Assistenz, Bürokraft, Mitarbeiter, Mitarbeiterin"),
        ("Trainee", "Anwärter, Anwärterin, Auszubildende, Praktikant, Praktikantin"),
    ],
    "startup": [
        ("Founder, C-level", "Founder, Co-Founder, CEO, CTO, COO, CFO, Chief"),
        ("VP", "VP, Vice President"),
        ("Head of", "Head of, Director"),
        ("Lead", "Lead, Team Lead"),
        ("Manager", "Manager"),
        ("Staff, principal", "Staff, Principal"),
        ("Senior", "Senior, Sr"),
        ("Mid-level", "Engineer, Designer, Developer, Specialist, Analyst, Executive"),
        ("Junior", "Junior, Associate"),
        ("Intern, working student", "Intern, Working Student, Werkstudent, Praktikant"),
    ],
}

# What a preset says beyond its levels: which levels lead (and hold only
# with reports), where a leader without reports lands, which level the
# grade ladder moves and how. A consulting or public-service title names a
# career step, not a line: its levels need no reports and know no ladder.
LADDERS = {
    "corporate": {},
    "consulting": {"needs_reports": [], "no_reports_level": 6, "grade_level": 6, "grades": ""},
    "public": {"needs_reports": [2, 3, 4], "no_reports_level": 6, "grade_level": 8, "grades": ""},
    "startup": {"needs_reports": [3, 5], "no_reports_level": 8, "grade_level": 8},
}

DEFAULT_MODEL = {
    "preset": "corporate",
    "levels": [{"name": n, "rules": r} for n, r in CORPORATE],
    # Words that never decide a level – regions, gender suffixes.
    "never": "EMEA, DACH, APJ, Americas, (m/w/d), (w/m/d), (f/m/d)",
    # Titles that name no member of the organization: counted apart.
    "outside": "Contractor, Contingent Worker, Shared Mailbox, Room Mailbox, Resource Mailbox, "
               "Meeting Room, Conference Room",
    # The grade ladder after a role ("Engineer III") – moves grade_level only.
    "grades": "I = 9, II = 8, III = 7, IV = 6",
    "grade_level": 8,
    # Leadership levels that hold only for someone with reports in the tree.
    "needs_reports": [3, 4, 5],
    "no_reports_level": 8,
}

ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
_DASHES = re.compile(r"\s*[‐‑‒–—―]\s*")
_SPACES = re.compile(r"\s+")
_TAIL_BRACKET = re.compile(r"\s*\([^()]*\)\s*$")
_TAIL_CODE = re.compile(r"\s*-\s*([A-Z]{2,5})\s*$")


# ---------------------------------------------------------------------------
# The role model: what the settings hold, made safe
# ---------------------------------------------------------------------------
def words(text):
    return [w.strip() for w in str(text or "").split(",") if w.strip()]


def clean_model(raw):
    """A role model as the settings may hold it – anything else falls back
    to the default part by part. Levels: exactly ten, each a name and its
    rule words."""
    raw = raw if isinstance(raw, dict) else {}
    preset = raw.get("preset") if isinstance(raw.get("preset"), str) else None
    # What the model does not say comes from its own preset – the numbers
    # of one ladder mean other levels in another.
    out = preset_model(preset if preset in PRESETS else "corporate")
    if preset == "own":
        out["preset"] = "own"
    levels = raw.get("levels")
    if isinstance(levels, list) and len(levels) == LEVELS and all(isinstance(x, dict) for x in levels):
        out["levels"] = [{"name": str(x.get("name") or f"Level {i + 1}")[:80],
                          "rules": str(x.get("rules") or "")[:2000]}
                         for i, x in enumerate(levels)]
    for key in ("never", "outside", "grades"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key][:2000]
    for key in ("grade_level", "no_reports_level"):
        if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) \
                and 1 <= raw[key] <= LEVELS:
            out[key] = raw[key]
    if isinstance(raw.get("needs_reports"), list):
        out["needs_reports"] = sorted({n for n in raw["needs_reports"]
                                       if isinstance(n, int) and not isinstance(n, bool)
                                       and 1 <= n <= LEVELS})
    return out


def clean_overrides(raw):
    """{group key: level 1–10 or "outside"} – what the review dialog sets."""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k).strip().casefold()[:300]
            if not key:
                continue
            if v == "outside" or (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= LEVELS):
                out[key] = v
    return out


def preset_model(name):
    model = json.loads(json.dumps(DEFAULT_MODEL))
    model["preset"] = name
    model["levels"] = [{"name": n, "rules": r} for n, r in PRESETS[name]]
    model.update(json.loads(json.dumps(LADDERS.get(name, {}))))
    return model


def model_checksum(model, overrides):
    text = json.dumps([model, overrides], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _grades(text):
    """"I = 9, II = 8" -> {"i": 9, "ii": 8}; a token's own level 1–10."""
    out = {}
    for part in words(text):
        name, sep, level = part.partition("=")
        if not sep:
            name, sep, level = part.partition("→")
        try:
            n = int(level.strip())
        except ValueError:
            continue
        if name.strip() and 1 <= n <= LEVELS:
            out[name.strip().casefold()] = n
    return out


def _pattern(word):
    return re.compile(r"(?<![\w])" + re.escape(word.casefold()) + r"(?![\w])")


class Model:
    """A role model compiled for matching."""

    def __init__(self, model=None, overrides=None):
        self.model = clean_model(model)
        self.overrides = clean_overrides(overrides)
        self.names = [x["name"] for x in self.model["levels"]]
        self.rules = [(len(w.split()), i + 1, w, _pattern(w))
                      for i, x in enumerate(self.model["levels"]) for w in words(x["rules"])]
        self.rule_words = {w.casefold() for _n, _l, w, _p in self.rules}
        self.never = [_pattern(w) for w in words(self.model["never"])]
        self.outside = [_pattern(w) for w in words(self.model["outside"])]
        self.grades = _grades(self.model["grades"])
        self.needs_reports = set(self.model["needs_reports"])

    def normalize(self, title):
        """The title as its group: dashes alike, spaces single, a trailing
        country or region code and a trailing bracketed remark gone –
        repeatedly, as some titles carry both."""
        t = _SPACES.sub(" ", _DASHES.sub(" - ", str(title or "")).strip())
        while True:
            before = t
            t = _TAIL_BRACKET.sub("", t)
            m = _TAIL_CODE.search(t)
            # A grade is no country code, and neither is a rule's own
            # word: "Co-CEO" keeps its CEO, "Coordinator-CH" loses its CH.
            if m and m.group(1).casefold() not in ROMAN and m.group(1).casefold() not in self.rule_words:
                t = t[:m.start()]
            t = t.rstrip(" -,")
            if t == before:
                return t

    def key(self, title):
        return self.normalize(title).casefold()

    def is_outside(self, title):
        t = str(title or "").casefold()
        return any(p.search(t) for p in self.outside)

    def rule_level(self, title):
        """(level, rule word) by the rules alone, or (None, None)."""
        t = self.normalize(title).casefold()
        for p in self.never:
            t = p.sub(" ", t)
        best = None
        for n_words, level, word, pattern in self.rules:
            m = pattern.search(t)
            if m:
                rank = (-m.start(), n_words, -level)
                if best is None or rank > best[0]:
                    best = (rank, level, word)
        if best is None:
            return None, None
        return self._graded(best[1], t), best[2]

    def _graded(self, level, text):
        """The grade ladder: a grade after the role moves the plain
        professional level; the last grade in the title counts."""
        if level == self.model["grade_level"] and self.grades:
            for token in re.findall(r"\w+", text):
                if token in self.grades:
                    level = self.grades[token]
        return level

    def place(self, title, leads):
        """(level or None, source) for one person: "hand", "rule", "tree"
        (a leadership title without reports), or None with "untitled",
        "outside", "unassigned"."""
        if not str(title or "").strip():
            return None, "untitled"
        key = self.key(title)
        if key in self.overrides:
            v = self.overrides[key]
            return (None, "outside") if v == "outside" else (v, "hand")
        if self.is_outside(title):
            return None, "outside"
        level, _word = self.rule_level(title)
        if level is None:
            return None, "unassigned"
        if level in self.needs_reports and not leads:
            return self._graded(self.model["no_reports_level"], self.normalize(title).casefold()), "tree"
        return level, "rule"


# ---------------------------------------------------------------------------
# The tree: who leads whom, how deep
# ---------------------------------------------------------------------------
def _active(org):
    return [pid for pid in org.people if pid not in org.disabled]


def direct_reports(org):
    """Active person -> active people reporting to them directly (a
    disabled manager in between hands their reports up, as the line
    stands)."""
    out = defaultdict(int)
    for pid in _active(org):
        boss = org.manager(pid)
        seen = {pid}
        while boss is not None and boss in org.disabled and boss not in seen:
            seen.add(boss)
            boss = org.manager(boss)
        if boss is not None and boss not in org.disabled:
            out[boss] += 1
    return out


def depth(org, pid):
    return sum(1 for i in org.chain(pid) if i not in org.disabled)


def figures(org, root=None):
    """The organization's health in numbers, from the tree alone – of the
    whole, or of the part from `root` down (its depths then count from
    `root`)."""
    active = _active(org)
    base = 0
    if root is not None:
        part = {root} | {pid for pid, _lv in org.below(root)}
        active = [pid for pid in active if pid in part]
        base = depth(org, root)
    reports = direct_reports(org)
    leaders = [pid for pid in active if reports.get(pid)]
    spans = sorted(reports[pid] for pid in leaders)
    depths = [depth(org, pid) - base for pid in active]
    n = len(active)
    return {
        "people": n,
        "leaders": len(leaders),
        "leader_share": round(len(leaders) / n, 4) if n else 0,
        "span_median": statistics.median(spans) if spans else 0,
        "span_mean": round(statistics.fmean(spans), 2) if spans else 0,
        "span_max": spans[-1] if spans else 0,
        "span_one": sum(1 for s in spans if s == 1),
        "span_wide": sum(1 for s in spans if s > 12),
        "people_per_leader": round((n - len(leaders)) / len(leaders), 2) if leaders else 0,
        "layers": (max(depths) + 1) if depths else 0,
    }


# ---------------------------------------------------------------------------
# The pyramid
# ---------------------------------------------------------------------------
def analyse(org, model=None, overrides=None):
    """The pyramid of one version: per level how many by rule, by hand and
    moved by the tree; the three pots beside it; the tree's own layers;
    the figures; and where title and tree disagree – numbers only."""
    m = Model(model, overrides)
    reports = direct_reports(org)
    levels = [{"level": i + 1, "name": m.names[i], "rule": 0, "hand": 0, "tree": 0}
              for i in range(LEVELS)]
    pots = Counter()
    tree = [0] * LEVELS
    disagree = set()
    groups = set()
    for pid in _active(org):
        title = org.people[pid].get("title")
        leads = bool(reports.get(pid))
        level, source = m.place(title, leads)
        d = depth(org, pid)
        tree[min(d, LEVELS - 1)] += 1
        if level is None:
            pots[source] += 1
            continue
        groups.add(m.key(title))
        levels[level - 1][source] += 1
        # Title and tree apart: a leadership title without reports, or
        # people led under a title the rules call a plain professional.
        if source == "tree" or (source == "rule" and leads and level >= m.model["grade_level"]):
            disagree.add(m.key(title))
    for x in levels:
        x["count"] = x["rule"] + x["hand"] + x["tree"]
    return {"levels": levels, "untitled": pots["untitled"], "outside": pots["outside"],
            "unassigned": pots["unassigned"], "tree": tree, "disagree": len(disagree),
            "figures": figures(org), "model": model_checksum(m.model, m.overrides)}


def titles(org, model=None, overrides=None):
    """The review dialog's list: every cleaned title once, with its holders,
    the level it takes and why, how many of them lead somebody, the median
    depth – and the most common words of the titles no rule covers, to
    make rules from. This is the one answer that carries titles: asked by
    the page's dialog, never polled, never handed to an MCP client."""
    m = Model(model, overrides)
    reports = direct_reports(org)
    by_key = {}
    for pid in _active(org):
        title = org.people[pid].get("title")
        if not str(title or "").strip():
            continue
        key = m.key(title)
        g = by_key.setdefault(key, {"key": key, "title": m.normalize(title), "variants": set(),
                                    "people": 0, "leading": 0, "depths": [],
                                    "levels": Counter(), "outside": False})
        g["variants"].add(title)
        g["people"] += 1
        leads = bool(reports.get(pid))
        g["leading"] += leads
        g["depths"].append(depth(org, pid))
        level, source = m.place(title, leads)
        if source == "outside":
            g["outside"] = True
        g["levels"][(level, source)] += 1
    items = []
    for g in by_key.values():
        (level, source), _n = g["levels"].most_common(1)[0]
        rule_level, rule_word = m.rule_level(g["title"])
        items.append({"key": g["key"], "title": g["title"], "variants": len(g["variants"]),
                      "people": g["people"], "leading": g["leading"],
                      "depth": statistics.median(g["depths"]),
                      "level": level, "source": source, "rule_level": rule_level,
                      "rule": rule_word, "mixed": len(g["levels"]) > 1})
    items.sort(key=lambda x: (-x["people"], x["title"].casefold()))
    return {"items": items, "words": suggestions(items, m)}


def suggestions(items, model):
    """The words and word pairs most common in the titles no rule covers,
    by how many people hold such a title – what a rule would catch."""
    known = {w.casefold() for _n, _l, w, _p in model.rules}
    stop = {"and", "of", "the", "for", "und", "der", "die", "das", "für", "&", "-"}
    count = defaultdict(lambda: [0, 0])
    for x in items:
        if x["source"] != "unassigned":
            continue
        tokens = [t for t in re.findall(r"[\w.&+]+", x["title"].casefold())
                  if t not in stop and t not in ROMAN and not t.isdigit() and len(t) > 1]
        seen = set(tokens) | {" ".join(p) for p in zip(tokens, tokens[1:], strict=False)}
        for w in seen - known:
            count[w][0] += 1
            count[w][1] += x["people"]
    ranked = sorted(count.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))
    return [{"word": w, "titles": t, "people": p} for w, (t, p) in ranked[:30]]


def widest(org, n=10):
    """The leaders with the most direct reports: (id, reports), widest first."""
    reports = direct_reports(org)
    return sorted(((pid, k) for pid, k in reports.items() if pid not in org.disabled),
                  key=lambda x: (-x[1], x[0]))[:n]
