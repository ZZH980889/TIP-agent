import sys

for _s in ("stdout", "stderr"):
    _st = getattr(sys, _s, None)
    if _st is not None and hasattr(_st, "reconfigure"):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
import os
import re
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple, Any, Set
import openpyxl
from openai import OpenAI

VERSION = "V4DIAG"
VERSION_DESC = "V4ABCD diagnostic version (unchanged decision logic plus purchase records; also used for repeated V4 runs)"
TOPK = int(os.getenv("WSPRO_TOPK", "6"))
MAX_SKIP = int(os.getenv("WSPRO_MAX_SKIP", "2"))
CLOSE_RESERVE = int(os.getenv("WSPRO_CLOSE_RESERVE", "6"))
MAX_REQUERY = int(os.getenv("WSPRO_MAX_REQUERY", "2"))
MAX_SUBPAGE_PER_ITEM = 1
RESULT_DETAIL_N = int(os.getenv("WSPRO_RESULT_DETAIL_N", "5"))
DEADLOCK_K = 3
QUERY_MAX_WORDS = int(os.getenv("WSPRO_QUERY_MAX_WORDS", "10"))
MODE_DESC = {
    "PRO": "Domain-specific agent: think + intent-direct execution + real-probe step1 + structured step2",
    "PRO_NOPROBE": "Ablation: no real probe in step1 (result titles only; no product-page option retrieval)",
    "PRO_NOSTEP2": "Ablation: disable step2 assessment (select directly from real pages and checklists)",
    "PRO_NOPARSE": "Ablation: no instruction parsing (pass the full instruction to the prompt; approximately ReAct's information setting)",
}


@dataclass
class StepRecord:
    kind: str
    text: str


@dataclass
class WorldModelState:
    goal: str = ""
    observations: List[Tuple[str, str]] = field(default_factory=list)
    knowledge: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        k = self.knowledge
        if "tried_actions" not in k:
            k["tried_actions"] = set()
        if "clicked_options" not in k:
            k["clicked_options"] = {}
        if "subpage_views" not in k:
            k["subpage_views"] = {}
        if "queries_issued" not in k:
            k["queries_issued"] = []
        if "skips_used" not in k:
            k["skips_used"] = 0
        if "filter_principles" not in k:
            k["filter_principles"] = [
                "[attribute_matches=N] counts overlaps between the product title and required task terms; higher N makes a candidate more worth inspecting first.",
                "Give highest priority to titles that explicitly contain the required color, size, or material.",
                "Rank clearly wrong categories lower (e.g., men's items when women's are requested, slippers when shirts are requested). Different wording does NOT imply a wrong category; graphic T-shirts and sets often have titles unlike their category descriptions.",
                "Products priced above the budget may be ranked lower, but need not be excluded entirely; sometimes the lower end of a price range is affordable.",
                "A candidate marked [verified] has already been opened and inspected; do not probe it again.",
            ]


@dataclass
class StuckState:
    is_stuck: bool = False
    stuck_duration: int = 0
    stuck_actions: List[str] = field(default_factory=list)


@dataclass
class Requirement:
    key: str
    value: str
    hard: bool = True

    def __repr__(self) -> str:
        return f"{self.key}={self.value}"


@dataclass
class ParsedTask:
    raw: str = ""
    price_max: Optional[float] = None
    attrs: Dict[str, str] = field(default_factory=dict)
    keywords: List[str] = field(default_factory=list)
    category: str = ""

    def hard_reqs(self) -> List[str]:
        out = [f"{k}={v}" for k, v in self.attrs.items()]
        if self.price_max is not None:
            out.append(f"price<={self.price_max}")
        return out

    def brief(self) -> str:
        return f"Category-related terms: {' '.join(self.keywords) or '(none)'}\n  Explicitly required attributes: {self.attrs or '(none)'}\n  Price limit: {(self.price_max if self.price_max is not None else '(none)')}"

    def __repr__(self) -> str:
        return f"ParsedTask(attrs={self.attrs}, price_max={self.price_max}, keywords={self.keywords})"


@dataclass
class Product:
    asin: str
    title: str = ""
    price_low: Optional[float] = None
    price_high: Optional[float] = None

    @property
    def price(self) -> Optional[float]:
        return self.price_low

    def __repr__(self) -> str:
        return f"{self.asin}({self.title[:26]!r}, ${self.price_low})"


@dataclass
class ItemPage:
    asin: str = ""
    title: str = ""
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    option_groups: Dict[str, List[str]] = field(default_factory=dict)
    has_buy: bool = False
    subpages: List[str] = field(default_factory=list)

    def all_options(self) -> List[str]:
        return [o for g in self.option_groups.values() for o in g]


@dataclass
class MatchResult:
    asin: str = ""
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    matched_soft: List[str] = field(default_factory=list)
    chosen_options: Dict[str, str] = field(default_factory=dict)
    price_ok: Optional[bool] = None
    score: float = 0.0

    @property
    def hard_missing(self) -> int:
        return len(self.missing)

    def unknown_price_risk(self) -> bool:
        return self.price_ok is None and any(
            (x.startswith("price<=") for x in self.unknown)
        )

    def summary(self) -> str:
        tot = (
            len(self.matched)
            + len(self.missing)
            + len(self.unknown)
            + len(self.matched_soft)
        )
        soft = (
            f"Title-only matches (no selectable option): {', '.join(self.matched_soft)}\n"
            if self.matched_soft
            else ""
        )
        return (
            f"MATCHED: {', '.join(self.matched) or 'none'}\nMISSING: {', '.join(self.missing) or 'none'}\nUNKNOWN: {', '.join(self.unknown) or 'none'}\n"
            + soft
            + f"MATCH_SCORE: {len(self.matched)}/{tot}\nPRICE_OK: {self.price_ok}"
        )


def verdict_category_wrong(verdict: str) -> bool:
    if not verdict:
        return False
    m = re.search("CATEGORY\\s*:\\s*([^|\\n]+)", verdict)
    return bool(m and "WRONG" in m.group(1))


@dataclass
class Ledger:
    entries: Dict[str, Tuple[ItemPage, MatchResult]] = field(default_factory=dict)
    verdicts: Dict[str, str] = field(default_factory=dict)

    def add(self, page: ItemPage, res: MatchResult, verdict: str = "") -> None:
        if page.asin:
            self.entries[page.asin] = (page, res)
            if verdict:
                self.verdicts[page.asin] = verdict

    def has(self, asin: str) -> bool:
        return (asin or "").lower() in self.entries

    def category_wrong(self, asin: str) -> bool:
        return verdict_category_wrong(self.verdicts.get((asin or "").lower(), ""))

    def best(
        self, exclude: Optional[Set[str]] = None, allow: Optional[Set[str]] = None
    ) -> Optional[Tuple[ItemPage, MatchResult]]:
        ex = exclude or set()
        pool = [(k, v) for k, v in self.entries.items() if k not in ex]
        if allow is not None:
            pool = [(k, v) for k, v in pool if k in allow]
        if not pool:
            return None
        return min(
            pool,
            key=lambda kv: (
                kv[1][1].hard_missing,
                -kv[1][1].score,
                self.category_wrong(kv[0]),
            ),
        )[1]

    def render(self, top_n: int = 8) -> str:
        if not self.entries:
            return "(no products have been verified through real inspection yet)"
        items = sorted(
            self.entries.values(), key=lambda kv: (kv[1].hard_missing, -kv[1].score)
        )[:top_n]
        lines = []
        for page, res in items:
            lines.append(f"· {page.asin}  ${page.price_low}  {page.title[:60]}")
            lines.append(f"    Matched: {', '.join(res.matched) or 'none'}")
            lines.append(f"    Missing: {', '.join(res.missing) or 'none'}")
            v = self.verdicts.get(page.asin, "")
            if v:
                lines.append(f"    Step2 verdict: {v[:120]}")
        return "\n".join(lines)


def build_client() -> OpenAI:
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.chatanywhere.tech/v1")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required")
    return OpenAI(base_url=base_url, api_key=api_key)


def llm(prompt: str, client: OpenAI, model: str, max_tokens: int) -> str:
    for attempt in range(2):
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                max_tokens=max_tokens,
                timeout=60.0,
            )
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"LLM call failed (attempt {attempt + 1}/2): {str(e)[:100]}")
            if attempt == 0:
                time.sleep(2)
                continue
            return ""
    return ""


_ATTR_KEYS = (
    "color",
    "size",
    "material",
    "fit",
    "fit type",
    "style",
    "flavor",
    "scent",
    "pattern",
    "type",
    "capacity",
    "quantity",
    "count",
    "width",
    "length",
    "wattage",
    "voltage",
    "finish",
    "shade",
    "model",
    "brand",
    "package",
    "pack",
    "item shape",
    "shape",
    "special size",
    "closure",
    "occasion",
    "heel height",
    "heel type",
    "toe shape",
    "neckline",
    "sleeve type",
)
_PRICE_PATTERNS = (
    re.compile("price\\s+(?:lower|less)\\s+than\\s+\\$?([\\d.]+)", re.I),
    re.compile(
        "(?:under|below|less\\s+than|at\\s+most|no\\s+more\\s+than)\\s+\\$?([\\d.]+)",
        re.I,
    ),
    re.compile("\\$\\s*([\\d.]+)\\s*(?:or\\s+less|max|maximum)", re.I),
    re.compile("budget\\s+(?:of\\s+)?\\$?([\\d.]+)", re.I),
)
_STOPWORDS = {
    "i",
    "im",
    "i'm",
    "me",
    "my",
    "we",
    "our",
    "you",
    "your",
    "am",
    "is",
    "are",
    "was",
    "be",
    "been",
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "if",
    "of",
    "for",
    "to",
    "in",
    "on",
    "at",
    "by",
    "with",
    "without",
    "from",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "as",
    "so",
    "than",
    "then",
    "there",
    "here",
    "would",
    "could",
    "should",
    "will",
    "shall",
    "can",
    "may",
    "want",
    "wanted",
    "wants",
    "need",
    "needed",
    "needs",
    "looking",
    "look",
    "like",
    "please",
    "also",
    "some",
    "any",
    "very",
    "really",
    "get",
    "buy",
    "find",
    "searching",
    "search",
    "prefer",
    "preferably",
    "show",
    "give",
    "dollars",
    "dollar",
    "price",
    "priced",
    "cheaper",
    "lower",
    "less",
    "under",
    "below",
    "cost",
    "costs",
    "usd",
    "one",
    "something",
    "item",
    "product",
    "size",
    "sizes",
    "color",
    "colour",
    "colors",
    "material",
    "style",
    "quantity",
    "count",
    "type",
    "pack",
    "package",
    "flavor",
    "scent",
    "shade",
    "finish",
    "model",
    "easy",
    "care",
    "machine",
    "washable",
    "wash",
    "tumble",
    "dry",
    "quality",
    "nice",
    "good",
    "great",
    "best",
    "comfortable",
    "comfy",
    "officially",
    "licensed",
    "day",
    "comfort",
    "everyday",
    "daily",
    "wear",
    "occasion",
    "use",
    "used",
}
_PRICE_CUE = {
    "under",
    "below",
    "lower",
    "less",
    "than",
    "price",
    "priced",
    "cost",
    "costs",
    "most",
    "max",
    "maximum",
    "budget",
    "cheaper",
    "within",
    "dollars",
    "dollar",
    "usd",
}
_DECIMAL_RE = re.compile("^\\$?\\d+\\.\\d+$")
_NUM_RE = re.compile("^\\$?\\d+$")


def parse_instruction(instruction: str) -> ParsedTask:
    task = ParsedTask(raw=instruction or "")
    text = re.sub(
        "^\\s*instruction\\s*:\\s*", "", instruction or "", flags=re.I
    ).strip()
    for pat in _PRICE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                task.price_max = float(m.group(1))
            except ValueError:
                pass
            text = text[: m.start()] + " " + text[m.end() :]
            break
    text = re.sub("\\bdollars?\\b", " ", text, flags=re.I)
    key_alt = "|".join(
        sorted((k.replace(" ", "\\s+") for k in _ATTR_KEYS), key=len, reverse=True)
    )
    attr_re = re.compile(
        f"\\b({key_alt})\\s*:\\s*(.*?)(?=\\s*,?\\s*and\\s+(?:{key_alt})\\s*:|$)", re.I
    )
    for m in attr_re.finditer(text):
        key = re.sub("\\s+", " ", m.group(1).strip().lower())
        val = m.group(2).strip().rstrip(".").strip()
        val = re.sub("\\s+and\\s*$", "", val, flags=re.I).strip().strip(" ,;.").strip()
        if val and key not in task.attrs:
            task.attrs[key] = val.lower()
    text = attr_re.sub(" ", text)
    raw_tokens = re.findall("[A-Za-z0-9][A-Za-z0-9'\\-\\.&]*", text)
    norm = [t.lower().strip(".") for t in raw_tokens]
    for i, tl in enumerate(norm):
        if not tl or _DECIMAL_RE.match(tl):
            continue
        if _NUM_RE.match(tl):
            prev = next((norm[j] for j in range(i - 1, -1, -1) if norm[j]), "")
            if tl.startswith("$") or prev in _PRICE_CUE:
                continue
        if tl in _STOPWORDS or tl in task.keywords:
            continue
        task.keywords.append(tl)
    if task.keywords:
        task.category = (
            " ".join(task.keywords[-2:])
            if len(task.keywords) >= 2
            else task.keywords[-1]
        )
    return task


def build_query(task: ParsedTask, variant: int = 0) -> str:
    attr_words: List[str] = []
    for k, v in task.attrs.items():
        if k == "price":
            continue
        for w in re.findall("[A-Za-z0-9][A-Za-z0-9'\\-]*", v):
            wl = w.lower()
            if wl not in _STOPWORDS and wl not in attr_words:
                attr_words.append(wl)
    if variant == 0:
        kw = [w for w in task.keywords if w not in attr_words]
        budget = max(0, QUERY_MAX_WORDS - len(attr_words))
        return " ".join(kw[:budget] + attr_words)
    if variant == 1:
        return " ".join(task.keywords[:8]) or " ".join(attr_words[:6])
    head = task.keywords[:2]
    tail = task.category.split()
    return " ".join(head + [w for w in tail if w not in head]) or (
        task.keywords[0] if task.keywords else task.raw[:40]
    )


_BARE_ASIN_RE = re.compile("^b0[0-9a-z]{8}$", re.IGNORECASE)
_PRICE_TOKEN_RE = re.compile("\\$\\s*([\\d.]+)")
_NAV_BUTTONS = {
    "back to search",
    "< prev",
    "<prev",
    "prev",
    "next >",
    "next",
    "buy now",
    "description",
    "features",
    "reviews",
    "attributes",
}
_SUBPAGES = {"description", "features", "reviews", "attributes"}


def _parse_prices(text: str) -> Tuple[Optional[float], Optional[float]]:
    nums = [float(x) for x in _PRICE_TOKEN_RE.findall(text or "")]
    if not nums:
        return (None, None)
    return (min(nums), max(nums))


def parse_results_page(obs: str) -> List[Product]:
    out: List[Product] = []
    if not obs:
        return out
    parts = [p.strip() for p in obs.split("[SEP]")]
    i = 0
    while i < len(parts):
        if _BARE_ASIN_RE.match(parts[i]):
            title = parts[i + 1] if i + 1 < len(parts) else ""
            price_txt = parts[i + 2] if i + 2 < len(parts) else ""
            lo, hi = _parse_prices(price_txt)
            if lo is None:
                lo, hi = _parse_prices(title)
            out.append(
                Product(asin=parts[i].lower(), title=title, price_low=lo, price_high=hi)
            )
            i += 3
            continue
        i += 1
    return out


def parse_item_page(obs: str, clickables: List[str], asin: str = "") -> ItemPage:
    page = ItemPage(asin=asin)
    if not obs:
        return page
    clickset = {c.strip().lower() for c in clickables or []}
    cur_group = ""
    titles: List[str] = []
    for p in [x.strip() for x in obs.split("[SEP]") if x.strip()]:
        pl = p.lower()
        if pl in _NAV_BUTTONS:
            if pl in _SUBPAGES:
                page.subpages.append(pl)
            if pl == "buy now":
                page.has_buy = True
            continue
        if pl.startswith("price"):
            page.price_low, page.price_high = _parse_prices(p)
            continue
        if pl.startswith("rating"):
            continue
        if pl in clickset:
            page.option_groups.setdefault(cur_group or "options", []).append(pl)
        elif len(p) <= 24 and (not _PRICE_TOKEN_RE.search(p)):
            cur_group = pl
        else:
            titles.append(p)
    if titles:
        page.title = max(titles, key=len)
    if page.price_low is None:
        page.price_low, page.price_high = _parse_prices(obs)
    return page


def render_observation(obs: str, clickables: List[str]) -> str:
    if not obs or "[SEP]" not in obs:
        return obs or ""
    clickset = {c.strip().lower() for c in clickables or []}
    lines: List[str] = []
    prod_cnt, suppress = (0, 0)
    for p in [x.strip() for x in obs.split("[SEP]")]:
        if not p:
            continue
        if _BARE_ASIN_RE.match(p):
            lines.append(f"[{p}]")
            prod_cnt += 1
            suppress = 2 if prod_cnt > RESULT_DETAIL_N else 0
            continue
        if suppress > 0:
            suppress -= 1
            continue
        lines.append(f"[{p}]" if p.lower() in clickset else p)
    return "\n".join(lines).strip() or obs


def _norm(s: str) -> str:
    return re.sub("[^a-z0-9]+", "", (s or "").lower())


def _tokens(s: str) -> Set[str]:
    return {t for t in re.split("[^a-z0-9]+", (s or "").lower()) if t}


def value_matches(want: str, candidates: List[str]) -> Tuple[bool, str, float]:
    w = _norm(want)
    if not w:
        return (False, "", 0.0)
    cands = [c for c in candidates or [] if c]
    for c in cands:
        if _norm(c) == w:
            return (True, c, 1.0)
    for c in cands:
        if w in _norm(c):
            return (True, c, 0.9)
    wt = _tokens(want)
    if wt:
        for c in cands:
            if wt <= _tokens(c):
                return (True, c, 0.8)
    return (False, "", 0.0)


def match_item(task: ParsedTask, page: ItemPage) -> MatchResult:
    res = MatchResult(asin=page.asin)
    groups = page.option_groups
    title_l = (page.title or "").lower()
    for key, want in task.attrs.items():
        gname = None
        for g in groups:
            if (
                _norm(g) == _norm(key)
                or _norm(key) in _norm(g)
                or _norm(g) in _norm(key)
            ):
                gname = g
                break
        if gname is not None:
            ok, hit, _ = value_matches(want, groups[gname])
            if ok:
                res.matched.append(f"{key}={want}")
                res.chosen_options[gname] = hit
            else:
                res.missing.append(f"{key}={want}")
            continue
        ok, hit, _ = value_matches(want, page.all_options())
        if ok:
            res.matched.append(f"{key}={want}")
            for g, opts in groups.items():
                if hit in opts:
                    res.chosen_options[g] = hit
                    break
        elif _norm(want) and _norm(want) in _norm(title_l):
            res.matched_soft.append(
                f"{key}={want}(title-only match; no selectable option)"
            )
            res.score -= 0.5
        else:
            res.missing.append(f"{key}={want}")
    if task.price_max is not None:
        lo, hi = (page.price_low, page.price_high)
        if lo is None:
            res.unknown.append(f"price<={task.price_max}")
        elif (hi if hi is not None else lo) <= task.price_max + 1e-09:
            res.matched.append(f"price<={task.price_max}")
            res.price_ok = True
        elif lo <= task.price_max + 1e-09:
            res.unknown.append(
                f"price<={task.price_max}(listed ${lo}~${hi}, higher-priced variants exceed the budget)"
            )
            res.price_ok = None
            res.score -= 1.0
        else:
            res.missing.append(f"price<={task.price_max}(actual ${lo})")
            res.price_ok = False
    covered = _norm(" ".join(task.attrs.values()))
    for w in task.keywords:
        if _norm(w) and _norm(w) in covered:
            continue
        (res.matched if _norm(w) in _norm(title_l) else res.unknown).append(w)
    n_has_group = sum(
        (
            1
            for key in task.attrs
            if any(
                (
                    _norm(g) == _norm(key)
                    or _norm(key) in _norm(g)
                    or _norm(g) in _norm(key)
                    for g in groups
                )
            )
        )
    )
    n_attr = max(1, len(task.attrs))
    opt_cover = len(res.chosen_options) / n_attr
    res.score += (
        len(res.matched) * 1.0
        - res.hard_missing * 3.0
        - len(res.unknown) * 0.2
        + n_has_group * 0.5
        + len(res.chosen_options) * 1.5
        + opt_cover * 3.0
    )
    if task.keywords:
        overlap = sum(
            (1 for w in task.keywords if _norm(w) and _norm(w) in _norm(title_l))
        )
        if overlap == 0:
            res.score -= 8.0
            res.missing.append(
                "Category mismatch (no keyword overlap between title and task)"
            )
        elif overlap == 1 and len(task.keywords) >= 3:
            res.score -= 2.0
    return res


def score_product_shallow(task: ParsedTask, prod: Product) -> float:
    s = 0.0
    t = _norm(prod.title)
    title_l = (prod.title or "").lower()
    for w in task.keywords:
        if _norm(w) and _norm(w) in t:
            s += 1.0
    for v in task.attrs.values():
        if _norm(v) and _norm(v) in t:
            s += 1.5
    if task.price_max is not None and prod.price is not None:
        s += 1.0 if prod.price <= task.price_max else -2.0
    task_text = " ".join(task.keywords).lower()
    W = re.compile("\\bwomen'?s?\\b|\\bgirls?\\b|\\bladies\\b", re.I)
    M = re.compile("\\bmen'?s?\\b|\\bboys?\\b", re.I)
    want_w, want_m = (bool(W.search(task_text)), bool(M.search(task_text)))
    has_w, has_m = (bool(W.search(title_l)), bool(M.search(title_l)))
    if want_w and (not want_m) and has_m and (not has_w):
        s -= 4.0
    elif want_m and (not want_w) and has_w and (not has_m):
        s -= 4.0
    return s


def best_option_for(group: str, options: List[str], task: ParsedTask) -> Optional[str]:
    for key, want in task.attrs.items():
        if (
            _norm(group) == _norm(key)
            or _norm(key) in _norm(group)
            or _norm(group) in _norm(key)
        ):
            ok, hit, _ = value_matches(want, options)
            if ok:
                return hit
    for want in task.attrs.values():
        ok, hit, _ = value_matches(want, options)
        if ok:
            return hit
    return None


def simulate_action(
    task: ParsedTask,
    env: "WebShopPro",
    asin: str,
    prod: Optional[Product],
    client: OpenAI,
    model: str,
    max_tokens: int,
    use_probe: bool = True,
    use_step2: bool = True,
) -> Tuple[Optional[ItemPage], Optional[MatchResult], str, int]:
    if use_probe:
        page = env.probe_item(asin)
        if page is None:
            return (None, None, "", 0)
        if prod is not None:
            page.title = page.title or prod.title
            if page.price_low is None:
                page.price_low, page.price_high = (prod.price_low, prod.price_high)
    else:
        if prod is None:
            return (None, None, "", 0)
        page = ItemPage(
            asin=asin,
            title=prod.title,
            price_low=prod.price_low,
            price_high=prod.price_high,
        )
    res = match_item(task, page)
    res.asin = page.asin
    if not use_step2:
        return (page, res, "", 0)
    if res.hard_missing == 0 and (not res.unknown_price_risk()):
        return (
            page,
            res,
            "VERDICT: FULL_MATCH | CATEGORY: CORRECT | Rule-based: all requirements can be fulfilled (no LLM call)",
            0,
        )
    if task.attrs and (not page.option_groups):
        return (
            page,
            res,
            "VERDICT: MISMATCH | CATEGORY: UNCERTAIN | Rule-based: the task requires specific attributes but this product has no selectable options (no LLM call)",
            0,
        )
    verdict, calls = llm_step2(task, page, res, client, model, max_tokens)
    return (page, res, verdict, calls)


def llm_step2(
    task: ParsedTask,
    page: ItemPage,
    res: MatchResult,
    client: OpenAI,
    model: str,
    max_tokens: int,
) -> Tuple[str, int]:
    opts = {
        g: v[:12] for g, v in page.option_groups.items()
    } or "(this product has no selectable options)"
    prompt = f"You are a shopping-task assessment expert. Below is the content obtained by ACTUALLY OPENING a candidate product page (facts, not guesses).\n\nOriginal task: {task.raw}\n\nCandidate product:\n  ID: {page.asin}\n  Title: {page.title}\n  Price: ${page.price_low}\n  Available options: {opts}\n\nRequirement-by-requirement checklist (computed by deterministic matching; these are facts):\n{res.summary()}\n\nAssess whether this candidate is worth buying. Scores accumulate PER REQUIREMENT: satisfying more requirements yields a higher score.\nJudge how many requirements this product can fulfill, rather than whether it is perfect. Focus on:\n1. **Whether available options cover the requested color/size/fit**. This is the most important criterion,\n   because scoring directly counts required options that were selected. An available matching option can earn that component.\n2. For MISSING items, distinguish a genuinely absent option from information merely omitted from the title; the latter may still be satisfied.\n3. UNKNOWN items such as machine wash / imported / classic fit are often on [features] or [description] subpages.\n   Their absence from the title does not imply failure.\n4. Treat the title category as a problem only when it EXPLICITLY EXCLUDES the requested category (e.g., shoes requested, throw pillow offered).\n   Important: product titles in this store often differ from category descriptions (graphic T-shirt titles name the design;\n   furniture-set titles name one piece). **If its options exactly cover the requested color and size,\n   it is likely the target product.** Do not judge the category as wrong merely because the title uses different wording.\n\nOutput one line only:\nVERDICT: <FULL_MATCH|NEAR_MATCH|PARTIAL_MATCH|MISMATCH> | CATEGORY: <CORRECT|WRONG|UNCERTAIN> | one-sentence reason\n"
    return ((llm(prompt, client, model, max_tokens) or "").strip(), 1)


def quick_filter_candidates(
    task: ParsedTask,
    products: List[Product],
    ledger: Ledger,
    target_count: int,
    client: OpenAI,
    model: str,
    max_tokens: int,
    current_think: str,
    principles: List[str],
) -> Tuple[List[Product], int]:
    fresh = [p for p in products if not ledger.has(p.asin)]
    if not fresh:
        return ([], 0)
    ranked = sorted(fresh, key=lambda p: -score_product_shallow(task, p))
    if len(ranked) <= target_count:
        return (ranked, 0)
    lines = []
    for i, p in enumerate(ranked, 1):
        hit = score_product_shallow(task, p)
        lines.append(
            f"{i}. {p.asin}  ${p.price_low}  [attribute_matches={hit:.1f}]  {p.title[:70]}"
        )
    prompt = f"You are a shopping-candidate filtering expert. Select the {target_count} products on the results page most worth ACTUALLY OPENING AND INSPECTING.\n(Opening each product costs resources, so ordering and selection matter.)\n\nOriginal task: {task.raw}\nStructured requirements:\n  {task.brief()}\n\nCurrent thought: {current_think or '(none)'}\n\nFiltering principles:\n{chr(10).join(('- ' + x for x in principles))}\n\nCandidates (preordered by attribute matches; order is not a verdict):\n{chr(10).join(lines)}\n\nOutput only the indices of the {target_count} products you want to inspect, separated by commas (e.g., 1,3,5,7), without explanation.\n"
    out = llm(prompt, client, model, max_tokens)
    try:
        idxs = [int(x.strip()) - 1 for x in (out or "").strip().split(",")]
        picked = [ranked[i] for i in idxs if 0 <= i < len(ranked)]
        if len(picked) >= max(1, target_count // 2):
            return (picked[:target_count], 1)
    except Exception:
        pass
    return (ranked[:target_count], 1)


def select_best_action(
    task: ParsedTask,
    ledger: Ledger,
    exclude: Set[str],
    client: OpenAI,
    model: str,
    max_tokens: int,
    clickable_now: Optional[Set[str]] = None,
) -> Tuple[Optional[str], int]:
    pool = [(k, v) for k, v in ledger.entries.items() if k not in exclude]
    if clickable_now is not None:
        pool = [(k, v) for k, v in pool if k in clickable_now]
    if not pool:
        return (None, 0)
    _clean = [(k, v) for k, v in pool if v[1].hard_missing == 0]
    if _clean:
        _clean.sort(key=lambda kv: -kv[1][1].score)
        print(
            f"   There are {len(_clean)} candidates without hard missing requirements; directly selecting the highest-scoring {_clean[0][0]}"
        )
        return (_clean[0][0], 0)
    _nwrong = sum((1 for k, _ in pool if ledger.category_wrong(k)))
    if _nwrong:
        print(
            f"   ℹ {_nwrong}/{len(pool)} candidates were judged CATEGORY: WRONG by step2; demoting rather than excluding them"
        )
    pool.sort(
        key=lambda kv: (
            kv[1][1].hard_missing,
            -kv[1][1].score,
            ledger.category_wrong(kv[0]),
        )
    )
    if len(pool) == 1:
        return (pool[0][0], 0)
    lines = []
    for i, (asin, (page, res)) in enumerate(pool, 1):
        opts = {g: v[:8] for g, v in page.option_groups.items()} or "(no options)"
        lines.append(f"{i}. {asin}  ${page.price_low}  {page.title[:66]}")
        lines.append(f"     Matched: {', '.join(res.matched) or 'none'}")
        lines.append(f"     Missing: {', '.join(res.missing) or 'none'}")
        lines.append(f"     Unknown: {', '.join(res.unknown) or 'none'}")
        lines.append(f"     Available options: {opts}")
        v = ledger.verdicts.get(asin, "")
        if v:
            lines.append(f"     step2: {v[:130]}")
    prompt = f"You are a shopping-decision expert. Every candidate below has been ACTUALLY OPENED; its content is factual.\n\nOriginal task: {task.raw}\nStructured requirements:\n  {task.brief()}\n\nCandidates (preordered by fewer missing requirements / more matches; order is not a verdict):\n{chr(10).join(lines)}\n\nSelection rules, in priority order:\n1. **Fewer missing requirements is better**. Scores accumulate by component (satisfied requirements / total requirements),\n   so the candidate missing the fewest requirements provides the highest currently attainable score.\n2. With equal missing counts, prefer the product that provides the required options (clickable color/size).\n   A product with no options cannot select the specified color/size and necessarily loses those components.\n3. If still tied, prefer the most matches.\n4. Reject by category only for an EXPLICIT mismatch (e.g., shoes requested, but a throw pillow / sea cucumber / speaker cable offered).\n   Different title wording does NOT imply the wrong category: graphic T-shirts may name a design and furniture sets a single item.\n   **Options covering the requested color and size are a strong positive signal.**\n\nOutput only the selected product ID (e.g., b08dk7s9b1), without explanation.\n"
    out = llm(prompt, client, model, max_tokens)
    m = re.search("b0[0-9a-z]{8}", out or "", re.I)
    if m and m.group(0).lower() in dict(pool):
        return (m.group(0).lower(), 1)
    return (pool[0][0], 1)


WEBSHOP_FEWSHOT = "Below is a complete successful shopping demonstration (its observation format matches the actual environment):\n\nInstruction: i would like a 3 ounce bottle of bright citrus deodorant for sensitive skin, and price lower than 50.00 dollars\n> think: Search the core product plus the most distinctive features only; leave price out of the query.\n> search[3 ounce bright citrus deodorant sensitive skin]\n[Back to Search]\nPage 1 (Total results: 50)\n[Next >]\n[B078GWRC1J]\nBright Citrus Deodorant by Earth Mama | Natural and Safe for Sensitive Skin, Pregnancy and Breastfeeding, Contains Organic Calendula 3-Ounce\n$10.99\n[B078GTKVXY]\nGinger Fresh Deodorant by Earth Mama | Natural and Safe for Sensitive Skin, Contains Organic Calendula 3-Ounce\n$10.99\n> think: B078GWRC1J is a bright citrus deodorant for sensitive skin under $50 and matches the category. Open it.\n> click[b078gwrc1j]\n[Back to Search]\n[< Prev]\nscent\n[assorted scents]\n[bright citrus]\n[calming lavender]\n[ginger fresh]\n[simply non-scents]\nsize\n[travel set (4-pack)]\n[3 ounce (pack of 1)]\n[3-ounce (2-pack)]\nBright Citrus Deodorant by Earth Mama 3-Ounce\nPrice: $10.99\nRating: N.A.\n[Description]\n[Features]\n[Reviews]\n[Buy Now]\n> think: The task wants bright citrus scent and a 3 ounce bottle. I must click the option text exactly as listed: scent 'bright citrus' and size '3 ounce (pack of 1)'. Click the scent first.\n> click[bright citrus]\nYou have clicked bright citrus.\n> think: Now select the exact size the task asks for.\n> click[3 ounce (pack of 1)]\nYou have clicked 3 ounce (pack of 1).\n> think: Both required options (bright citrus, 3 ounce (pack of 1)) are selected and the price is under $50. Buy it now.\n> click[buy now]\n\nLessons from this demonstration, especially query construction:\n- Search using only CATEGORY TERMS plus SPECIFIC ATTRIBUTES named in the task. The example retains 3 ounce / bright citrus /\n  deodorant / sensitive skin, described as six content words, and omits the price.\n- **Do not include care instructions or marketing phrases in search queries**: machine washable / easy care / tumble dry /\n  day comfort / everyday wear / moisture wicking rarely appear in product titles.\n  They dilute useful keywords and retrieve unrelated categories.\n- Do not reduce the query to one or two generic words such as search[shorts], which retrieves popular but unrelated products.\n- Product options appear as a group name such as \"scent\" followed by [bright citrus]. Click the option that EXACTLY matches\n  the task text (e.g., click[3 ounce (pack of 1)], not click[3-ounce (2-pack)]).\n- Result pages show detailed titles for only the first few products; the remaining IDs are still clickable.\n- Select every required option before click[buy now]. Purchasing is irreversible.\n\nImportant exception: this example is NOT representative in one respect.\n  Here, the target title explicitly includes the scent and size (Bright Citrus ... 3-Ounce),\n  so they can be checked from the title. **For most tasks, colors and sizes rarely appear in titles;\n  they are options INSIDE the product page.**\n  For example, a task may request color: acorn / size: 30w x 34l. No result title may contain acorn or\n  30w x 34l, but opening \"Wrangler Men's Relaxed Fit Jean\" reveals these options.\n  Therefore:\n  - Use titles to judge CATEGORY (jeans / shorts / polo / throw pillow).\n  - Colors, sizes, and fits REQUIRE opening the product page. Do not reject an item because the title omits them.\n  - **Do not paginate or search again just because no title lists the required color/size**. This is expected,\n    even on page 5. If page 1 has products in the right category, open them and inspect the options.\n"


def llm_think(
    task: ParsedTask,
    env: "WebShopPro",
    ledger: Ledger,
    history: str,
    client: OpenAI,
    model: str,
    max_tokens: int,
    hint: str = "",
) -> Tuple[str, str, int]:
    legal = env.legal_actions()
    legal_txt = (
        "\n".join((f"  {a}" for a in legal[:40]))
        or "  (no clickable buttons on this page)"
    )
    if len(legal) > 40:
        legal_txt += f"\n  ... ({len(legal)} in total; only the first 40 shown)"
    if env.search_available():
        legal_txt += "\n  search[...] -- This page has a search box; write your own keywords, e.g., search[women's mint shorts]"
    memo = ""
    if ledger.entries:
        memo = (
            "\n[Verified-fact memory] These products have been opened and inspected by the system, which has already returned from them.\nThese are facts and the products remain available. Do not spend further steps navigating back just to verify them again.\n"
            + ledger.render()
            + "\n"
        )
    picked = ""
    if env.page_type == PAGE_ITEM:
        done_opts = env.clicked_options_for(env.cur_asin)
        live = env.item_page
        should = match_item(task, live).chosen_options
        todo = {g: o for g, o in should.items() if o not in done_opts}
        picked = f"\n[Option state] Already clicked on this product page: {sorted(done_opts) or '(none clicked yet)'}\n  Important: this store leaves page text UNCHANGED and provides NO confirmation after an option click. This is normal; your click took effect. Do not click the same option again merely because no change is visible.\n  Still required by the task: {todo or '(all selected; ready to buy)'}\n  Note: leaving this product page (returning to results or searching again) invalidates selected options; reselect them when returning.\n"
    prompt = f"Complete a shopping task on a keyword-search shopping website. Execute only one action at each step.\n\n{WEBSHOP_FEWSHOT}\n========== YOUR TASK ==========\n\nOriginal task: {task.raw}\nStructured requirements parsed from the task (facts for reference):\n  {task.brief()}\n\nRecent history:\n{history or '(just started)'}\n\nCurrent page:\n{env.render()}\n{picked}{memo}\n[Actions actually executable at this step] (your action must be one of these):\n{legal_txt}\n{hint}\nKey points:\n- Retain category terms and specific attributes named in the task (color/size/material, etc.) in search queries. Remove price numbers and\n  care/marketing phrases (machine washable / easy care / tumble dry / day comfort / everyday wear),\n  which rarely appear in titles and dilute useful terms. Do not reduce queries to one or two generic words.\n- Result pages show detailed titles only for the first few products; the remaining product IDs are still clickable.\n- **Colors and sizes rarely appear in titles; they are options inside product pages.** Use titles to judge CATEGORY,\n  but open product pages to inspect color/size/fit. Do not paginate or search again because titles on this page\n  omit the required color or size; page 5 will usually do the same. Open relevant-category products on page 1.\n- On a product page, select the required options one by one, then buy. Purchasing is irreversible.\n- If neither title nor options reveal a required attribute (e.g., imported zipper / machine wash /\n  long lasting), inspect [features] or [description]; each of these subpages can be checked once.\n\nFirst output one reasoning line beginning with 'think:', then one line beginning with 'action:' specifying the concrete action you intend to execute immediately.\n"
    out = llm(prompt, client, model, max_tokens)
    think_text, intent = ("", "")
    for line in (out or "").splitlines():
        s = line.strip().lstrip(">-*#• ").strip()
        low = s.lower()
        if low.startswith("think:"):
            think_text = s[6:].strip()
        elif low.startswith("action:"):
            intent = s[7:].strip()
    if not think_text:
        think_text = (out or "").strip()
    if not intent:
        m = re.search("(search\\[[^\\]]*\\]|click\\[[^\\]]*\\])", out or "", re.I)
        if m:
            intent = m.group(1)
    return (think_text, intent, 1)


def land_intent(
    intent: str, legal: List[str], search_ok: bool = False
) -> Optional[str]:
    if not intent:
        return None
    s = intent.strip()
    sl = s.lower()
    if sl.startswith("search["):
        if not search_ok:
            return None
        kw = s[s.find("[") + 1 : s.rfind("]")].strip() if "[" in s and "]" in s else ""
        if (
            not kw
            or kw.startswith("<")
            or "keywords" in kw
            or ("your own" in kw.lower())
        ):
            return None
        return s
    for a in legal:
        if a.lower() == sl:
            return a
    m = re.search("b0[0-9a-z]{8}", sl, re.I)
    if m:
        want = f"click[{m.group(0).lower()}]"
        for a in legal:
            if a.lower() == want:
                return a
    contained = [a for a in legal if a.lower() in sl]
    if len(contained) == 1:
        return contained[0]
    if len(contained) > 1:
        contained.sort(key=len, reverse=True)
        if len(contained[0]) > len(contained[1]):
            return contained[0]
    return None


def apply_domain_pruning(
    state: WorldModelState, env: "WebShopPro", legal: List[str]
) -> Tuple[List[str], List[str]]:
    out, cut = ([], [])
    clicked = env.clicked_options_for(env.cur_asin)
    sv = state.knowledge["subpage_views"]
    for a in legal:
        m = re.match("^click\\[\\s*(.+?)\\s*\\]$", a.strip(), re.I)
        txt = m.group(1).strip().lower() if m else ""
        if (
            txt
            and txt not in _NAV_BUTTONS
            and (not _BARE_ASIN_RE.match(txt))
            and (txt in clicked)
        ):
            cut.append(f"R1 already selected option: {a}")
            continue
        if txt in _SUBPAGES:
            n = sv.get((env.cur_asin, txt), 0)
            if n >= MAX_SUBPAGE_PER_ITEM:
                cut.append(f"R2 [{txt}] already inspected {n} times: {a}")
                continue
        out.append(a)
    return (out or legal, cut)


def is_repeat_query(state: WorldModelState, query: str) -> bool:
    q = re.sub("\\s+", " ", (query or "").strip().lower())
    return q in {
        re.sub("\\s+", " ", x.strip().lower())
        for x in state.knowledge["queries_issued"]
    }


def detect_action_loop(last_actions: List[str], window: int = 6) -> bool:
    if len(last_actions) < window:
        return False
    recent = last_actions[-window:]
    if len(recent) >= 4:
        half = len(recent) // 2
        if recent[:half] == recent[half : half * 2]:
            return True
    return len(set(recent)) <= 2 and len(recent) >= 4


def update_stuck_state(stuck_state: StuckState, last_actions: List[str]) -> None:
    if detect_action_loop(last_actions, window=6):
        if not stuck_state.is_stuck:
            stuck_state.is_stuck = True
            stuck_state.stuck_duration = 4
            stuck_state.stuck_actions = list(set(last_actions[-6:]))
            print(
                f"Action cycle detected; forcing diversity for {stuck_state.stuck_duration} steps"
            )
    if stuck_state.is_stuck:
        stuck_state.stuck_duration -= 1
        if stuck_state.stuck_duration <= 0:
            stuck_state.is_stuck = False
            stuck_state.stuck_actions.clear()
            print("Stuck mode ended")


def re_review_deadlock(
    task: ParsedTask,
    env: "WebShopPro",
    ledger: Ledger,
    legal: List[str],
    stuck_intent: str,
    reason: str,
    history: str,
    client: OpenAI,
    model: str,
    max_tokens: int,
) -> Tuple[Optional[str], int]:
    if not legal:
        return (None, 0)
    memo = (
        "\nVerified products (these are facts and remain available):\n"
        + ledger.render()
        if ledger.entries
        else ""
    )
    actions_text = "\n".join((f"{i + 1}. {a}" for i, a in enumerate(legal)))
    prompt = f"You are an error-correction and replanning expert. The shopping agent is stuck: {reason}\nIt repeatedly insists on an approach without progressing. The approach may be wrong or lack a prerequisite.\n\nOriginal task: {task.raw}\nStructured requirements:\n  {task.brief()}\n\nRecent history:\n{history or '(none)'}\n{memo}\nIntent repeatedly proposed but not grounded: {stuck_intent}\n\nCurrent page: {env.page_type}\n[Currently executable actions] (select only from this list; indices start at 1):\n{actions_text}\n\nChoose between these two cases:\nA. If the intent is correct but lacks a prerequisite (e.g., searching while not on the search page requires\n   click[back to search] first), select the action that BEST SATISFIES that prerequisite.\nB. If the intent is misguided, select an action CLEARLY DIFFERENT from recent repeated attempts to break the deadlock.\n\nOutput only the selected action INDEX (1-{len(legal)}), without explanation.\n"
    out = llm(prompt, client, model, max_tokens)
    try:
        choice = int(re.search("\\d+", out or "").group())
        if 1 <= choice <= len(legal):
            return (legal[choice - 1], 1)
    except Exception:
        pass
    return (None, 1)


def commit_gate(
    task: ParsedTask,
    page: ItemPage,
    final: MatchResult,
    selected: Dict[str, str],
    unchecked: List[Product],
    skips_used: int,
    steps_left: int,
    client: OpenAI,
    model: str,
    max_tokens: int,
    ledger: Optional["Ledger"] = None,
) -> Tuple[bool, str, int]:
    if not unchecked:
        return (
            True,
            "No unverified candidates remain; no purchase yields zero -> buy",
            0,
        )
    if skips_used >= MAX_SKIP:
        return (True, f"Purchase veto limit reached: {MAX_SKIP} -> forcing purchase", 0)
    if steps_left < CLOSE_RESERVE:
        return (
            True,
            f"Only {steps_left} steps remain; insufficient time for further inspection -> buy",
            0,
        )
    if final.hard_missing == 0:
        return (
            True,
            "No hard missing requirements in the checklist -> buy directly (no further inspection needed)",
            0,
        )
    cur_shallow = score_product_shallow(
        task,
        Product(
            asin=page.asin,
            title=page.title,
            price_low=page.price_low,
            price_high=page.price_high,
        ),
    )
    better = [
        p for p in unchecked if score_product_shallow(task, p) > cur_shallow + 1e-09
    ]
    if not better:
        return (
            True,
            f"No unverified candidate has a higher shallow match score than the current product (current {cur_shallow:.1f}) -> buy",
            0,
        )
    better.sort(key=lambda p: -score_product_shallow(task, p))
    opts = {g: v[:10] for g, v in page.option_groups.items()} or "(none)"
    cand_txt = "\n".join(
        (
            f"  - {p.asin} ${p.price_low} [attribute_matches={score_product_shallow(task, p):.1f}] {p.title[:60]}"
            for p in better[:6]
        )
    )
    prompt = f"You are about to make an IRREVERSIBLE purchase. This is the single step that determines the score.\n\nOriginal task: {task.raw}\nStructured requirements:\n  {task.brief()}\n\nCurrent product:\n  ID: {page.asin}\n  Title: {page.title}\n  Price: ${page.price_low}\n  All available options: {opts}\n  Options to be selected: {selected or '(this product lacks the options required by the task)'}\n\nRequirement checklist:\n{final.summary()}\n\nThe following candidates are UNVERIFIED and have STRICTLY MORE title-attribute matches than the current product (current matches={cur_shallow:.1f}）:\n{cand_txt}\n(Remaining purchase vetoes: {MAX_SKIP - skips_used}; remaining steps: {steps_left})\n\nChoose one:\nBUY  -- These candidates are unlikely to satisfy the requirements missing from the current product, or their categories are clearly worse.\nLOOK -- A specific candidate above is clearly more likely to satisfy requirements missing from the current product.\n\nImportant facts that must guide your decision:\n- No purchase earns 0. Partial matches score satisfied requirements / total requirements, so buying an item missing one or two components is far better than buying nothing.\n- Leaving this product page INVALIDATES already selected colors/sizes; returning requires selecting them again. Vetoing is not free.\n- Choose LOOK only if you can identify a specific candidate likely to cover the missing requirements. Merely calling the current item imperfect is insufficient.\n\nOutput one line only:\nDECISION: <BUY|LOOK> | one-sentence reason\n"
    out = llm(prompt, client, model, max_tokens) or ""
    up = out.upper()
    if "LOOK" in up and "BUY" not in up.split("LOOK")[0]:
        return (False, out.strip(), 1)
    return (True, out.strip(), 1)


PAGE_SEARCH, PAGE_RESULTS, PAGE_ITEM, PAGE_SUB, PAGE_END = (
    "search",
    "results",
    "item",
    "item_sub",
    "end",
)


class WebShopPro:

    def __init__(self, env, state: Optional[WorldModelState] = None):
        self.env = env
        self.state = state
        self.instruction = ""
        self.task: ParsedTask = ParsedTask()
        self.page_type = PAGE_SEARCH
        self.cur_asin = ""
        self.last_obs = ""
        self.products: List[Product] = []
        self.item_page: ItemPage = ItemPage()
        self.probe_steps = 0
        self.env_steps = 0
        self._visit_asin = ""
        self.diag_last_fill: List[str] = []

    def _clickables(self) -> List[str]:
        try:
            av = self.env.get_available_actions()
            return (
                [str(c) for c in av.get("clickables", []) or []]
                if isinstance(av, dict)
                else []
            )
        except Exception:
            return []

    def _has_search_bar(self) -> bool:
        try:
            av = self.env.get_available_actions()
            return (
                bool(av.get("has_search_bar", False)) if isinstance(av, dict) else False
            )
        except Exception:
            return False

    def _strip(self, raw: str) -> str:
        if not raw or "[SEP]" not in raw or "Instruction:" not in raw:
            return raw or ""
        parts = [p.strip() for p in raw.split("[SEP]")]
        while parts and parts[0].lower() == "webshop":
            parts.pop(0)
        if parts and parts[0].lower().startswith("instruction:"):
            parts.pop(0)
            if parts:
                parts.pop(0)
        return " [SEP] ".join(parts).strip() or raw

    def render(self) -> str:
        return render_observation(self.last_obs, self._clickables())

    def legal_actions(self) -> List[str]:
        out: List[str] = []
        for c in self._clickables():
            cl = c.strip().lower()
            if cl == "search":
                continue
            out.append(f"click[{cl}]")
        return out

    def search_available(self) -> bool:
        return self.page_type == PAGE_SEARCH or self._has_search_bar()

    def clicked_options_for(self, asin: str) -> Set[str]:
        if self.state is None:
            return set()
        return self.state.knowledge["clicked_options"].setdefault(
            (asin or "").lower(), set()
        )

    def _sync(self, raw_obs: str):
        self.last_obs = self._strip(raw_obs)
        low = self.last_obs.lower()
        clk = self._clickables()
        clk_l = {c.strip().lower() for c in clk}
        if "thank you for shopping" in low:
            self.page_type = PAGE_END
        elif self._has_search_bar():
            self.page_type = PAGE_SEARCH
        elif "buy now" in clk_l:
            self.page_type = PAGE_ITEM
            self.item_page = parse_item_page(self.last_obs, clk, asin=self.cur_asin)
        elif any((_BARE_ASIN_RE.match(c.strip()) for c in clk)):
            self.page_type = PAGE_RESULTS
            self.products = parse_results_page(self.last_obs)
        else:
            self.page_type = PAGE_SUB
        self._sync_option_scope()

    def _sync_option_scope(self) -> None:
        if self.state is None:
            return
        book = self.state.knowledge["clicked_options"]
        if self.page_type == PAGE_SUB:
            return
        if self.page_type == PAGE_ITEM:
            if self._visit_asin and self._visit_asin != self.cur_asin:
                book.pop(self._visit_asin, None)
            self._visit_asin = self.cur_asin
            return
        if self._visit_asin:
            book.pop(self._visit_asin, None)
            self._visit_asin = ""

    def reset(self, session: int) -> str:
        try:
            raw, _ = self.env.reset(session=session)
        except TypeError:
            raw = self.env.reset(session=session)
        for attr in ("instruction_text", "goal"):
            v = getattr(self.env, attr, None)
            if isinstance(v, str) and v.strip():
                self.instruction = v.strip()
                break
        self.task = parse_instruction(self.instruction)
        self.page_type, self.cur_asin = (PAGE_SEARCH, "")
        self.products, self.item_page = ([], ItemPage())
        self.probe_steps = self.env_steps = 0
        self._visit_asin = ""
        self._sync(raw if isinstance(raw, str) else str(raw))
        return self.last_obs

    def step(self, action: str, count: bool = True) -> Tuple[str, float, bool]:
        al = action.strip().lower()
        m = re.match("^click\\[\\s*(.+?)\\s*\\]$", al)
        txt = m.group(1).strip() if m else ""
        if self.state is not None:
            if al.startswith("search["):
                self.state.knowledge["queries_issued"].append(action[7:-1])
            elif txt in _SUBPAGES:
                k = self.state.knowledge["subpage_views"]
                key = (self.cur_asin, txt)
                k[key] = k.get(key, 0) + 1
            elif txt and txt not in _NAV_BUTTONS and (not _BARE_ASIN_RE.match(txt)):
                self.clicked_options_for(self.cur_asin).add(txt)
        if txt and _BARE_ASIN_RE.match(txt):
            self.cur_asin = txt
        if al.startswith("search["):
            self.cur_asin = ""
        raw, reward, done, _ = self.env.step(action)
        if count:
            self.env_steps += 1
        else:
            self.probe_steps += 1
        self._sync(raw)
        return (self.last_obs, float(reward), bool(done))

    def ensure_options(
        self, task: ParsedTask, trace: Optional[List["StepRecord"]] = None
    ) -> List[str]:
        acted: List[str] = []
        if self.page_type != PAGE_ITEM:
            return acted
        want = match_item(task, self.item_page).chosen_options
        clicked = self.clicked_options_for(self.cur_asin)
        for g, o in want.items():
            if o in clicked:
                continue
            self.step(f"click[{o}]")
            acted.append(o)
            if trace is not None:
                trace.append(StepRecord(kind="act", text=f"click[{o}]"))
            print(f"   A: completing options [{g}] → click[{o}]")
            if self.page_type != PAGE_ITEM:
                break
        if not acted:
            print(
                f"   A: options complete ({sorted(want.values()) or 'this product requires no options'}）"
            )
        self.diag_last_fill = list(acted)
        return acted

    def probe_item(self, asin: str) -> Optional[ItemPage]:
        if self.page_type != PAGE_RESULTS:
            return None
        saved_asin = self.cur_asin
        self.step(f"click[{asin}]", count=False)
        if self.page_type != PAGE_ITEM:
            if self.page_type != PAGE_RESULTS:
                self.step("click[< prev]", count=False)
            self.cur_asin = saved_asin
            return None
        page = self.item_page
        self.step("click[< prev]", count=False)
        if self.page_type != PAGE_RESULTS:
            qs = self.state.knowledge["queries_issued"] if self.state else []
            if qs:
                print(
                    "Warning: probe rollback did not reach the results page; restoring it by repeating the last query"
                )
                self.step(f"search[{qs[-1]}]", count=False)
        self.cur_asin = saved_asin
        return page


def _diag_capture(
    diag: Dict[str, Any],
    env: "WebShopPro",
    task: ParsedTask,
    obs: str,
    reward: float,
    path: str,
) -> None:
    diag["path"] = path
    diag["reward"] = float(reward)
    diag["fill"] = "; ".join(getattr(env, "diag_last_fill", []) or [])
    diag["task_attrs"] = "; ".join((f"{k}={v}" for k, v in task.attrs.items()))
    diag["price_max"] = task.price_max
    diag["task_keywords"] = " ".join(task.keywords)
    m = re.search("options\\s*\\[SEP\\]\\s*(\\{.*?\\})", obs or "", re.S)
    diag["env_options"] = m.group(1) if m else ""
    m2 = re.search("asin\\s*\\[SEP\\]\\s*([A-Za-z0-9]+)", obs or "")
    if m2:
        diag["env_asin"] = m2.group(1).lower()
    if not diag.get("buy_asin"):
        diag["buy_asin"] = env.cur_asin
        diag["buy_title"] = (env.item_page.title or "")[:120]
        diag["buy_price"] = env.item_page.price_low
    if reward and 0 < reward <= 1:
        f = Fraction(reward).limit_denominator(20)
        if abs(float(f) - reward) < 0.002:
            diag["n_total"] = f.denominator
            diag["n_short"] = f.denominator - f.numerator


def run_episode(
    env: WebShopPro,
    client: OpenAI,
    model: str,
    max_tokens: int,
    max_steps: int = 50,
    mode: str = "PRO",
    diag: Optional[Dict[str, Any]] = None,
) -> Tuple[float, bool, List[StepRecord], int, int]:
    if diag is None:
        diag = {}
    use_probe = mode != "PRO_NOPROBE"
    use_step2 = mode != "PRO_NOSTEP2"
    no_parse = mode == "PRO_NOPARSE"
    state = WorldModelState(goal=env.instruction)
    env.state = state
    task = env.task
    if no_parse:
        task = ParsedTask(raw=env.instruction)
    stuck_state = StuckState()
    ledger = Ledger()
    trajectory: List[StepRecord] = []
    last_actions: List[str] = []
    history = ""
    llm_calls = 0
    last_intent, intent_repeat, optimistic_streak = ("", 0, 0)
    deferred_intent = ""
    skipped: Set[str] = set()
    suspended: Set[str] = set()
    print(f"Instruction: {env.instruction}")
    print(f"Parsed: {task}")
    print(f"Hard requirements: {task.hard_reqs()}")
    print(f"Maximum steps: {max_steps}  Mode: {mode}\n")
    for step in range(1, max_steps + 1):
        steps_left = max_steps - step + 1
        print(f"--- Step {step}/{max_steps} [{env.page_type}] ---")
        legal = env.legal_actions()
        search_ok = env.search_available()
        if not legal and (not search_ok):
            print("Warning: no executable actions; ending")
            break
        hint = ""
        if env.page_type == PAGE_RESULTS and ledger.entries:
            hint = "\nHint: the memory contains verified products. If one meets the requirements, click its ID directly. If none is suitable, use click[next >] to paginate or click[back to search] to search again.\n"
        think_text, intent, _c = llm_think(
            task, env, ledger, history, client, model, max_tokens, hint=hint
        )
        llm_calls += _c
        trajectory.append(StepRecord(kind="think", text=think_text))
        print(f"Thought: {think_text[:150]}")
        filtered, cut = apply_domain_pruning(state, env, legal)
        if suspended and env.page_type == PAGE_RESULTS:
            _f = [
                a
                for a in filtered
                if not any((a.lower() == f"click[{s}]" for s in suspended))
            ]
            if _f != filtered:
                print(
                    f"Suspension filter: {sorted(suspended)} not clickable at this step"
                )
                filtered = _f or filtered
        if cut:
            print(f"Domain pruning: {len(legal)} → {len(filtered)}  ({cut[0]})")
        if stuck_state.is_stuck:
            _f = [a for a in filtered if a not in stuck_state.stuck_actions]
            if _f:
                print(f"Stuck filter: remaining {len(_f)}/{len(filtered)}")
                filtered = _f
        same_intent = bool(intent) and intent == last_intent
        selected: Optional[str] = None
        took_intent = False
        is_optimistic = False
        if not stuck_state.is_stuck:
            selected = land_intent(intent, filtered, search_ok=search_ok)
            if selected and selected.lower().startswith("search["):
                q = selected[selected.find("[") + 1 : selected.rfind("]")]
                if is_repeat_query(state, q):
                    alt = ""
                    for v in range(0, 3):
                        cand = build_query(task, variant=v)
                        if cand and (not is_repeat_query(state, cand)):
                            alt = cand
                            break
                    if alt:
                        print(
                            f"R3: query already issued; using a new rule-based query: {alt[:60]}"
                        )
                        selected = f"search[{alt}]"
                    else:
                        print(
                            f"R3: query already issued but no better alternative exists -> allowing it"
                        )
        if (
            selected
            and use_probe
            and (env.page_type == PAGE_RESULTS)
            and re.match("^click\\[b0[0-9a-z]{8}\\]$", selected.strip(), re.I)
        ):
            _a = selected.strip()[6:-1].lower()
            if ledger.has(_a):
                print(
                    f"Candidate already verified (facts in memory); retaining direct execution: {selected}"
                )
            else:
                print(
                    f"Intent-direct execution deferred for real-probe comparison: {selected} -> fallback candidate comparison"
                )
                deferred_intent = selected
                selected = None
        if selected and use_probe and (env.page_type == PAGE_RESULTS):
            _sl = selected.strip().lower()
            _leaving = _sl in (
                "click[next >]",
                "click[next>]",
                "click[back to search]",
            ) or _sl.startswith("search[")
            if _leaving:
                _unchecked = [
                    p
                    for p in env.products
                    if not ledger.has(p.asin) and p.asin not in skipped
                ]
                if _unchecked:
                    print(
                        f"R4: this page still has {len(_unchecked)} unverified candidates (colors/sizes are inside product pages, not titles) -> probe before leaving; blocking {selected}"
                    )
                    deferred_intent = ""
                    selected = None
        if selected and re.match(
            "^click\\[\\s*buy\\s*now\\s*\\]$", selected.strip(), re.I
        ):
            live = env.item_page
            final = match_item(task, live)
            print(f"--- Pre-purchase checklist ---\n{final.summary()}")
            diag["check_matched"] = "; ".join(final.matched)
            diag["check_missing"] = "; ".join(final.missing)
            diag["check_unknown"] = "; ".join(final.unknown)
            diag["check_soft"] = "; ".join(getattr(final, "matched_soft", []))
            diag["buy_asin"] = live.asin
            diag["buy_title"] = (live.title or "")[:120]
            diag["buy_price"] = live.price_low
            print(
                f"Selected options: {sorted(env.clicked_options_for(env.cur_asin)) or '(none)'}"
            )
            unchecked = [
                p
                for p in env.products
                if not ledger.has(p.asin) and p.asin not in skipped
            ]
            ok, why, _c = commit_gate(
                task,
                live,
                final,
                {g: o for g, o in final.chosen_options.items()},
                unchecked,
                state.knowledge["skips_used"],
                steps_left,
                client,
                model,
                max_tokens,
                ledger=ledger,
            )
            llm_calls += _c
            print(f"🚦 {why[:170]}")
            if not ok:
                state.knowledge["skips_used"] += 1
                skipped.add(env.cur_asin)
                suspended.add(env.cur_asin)
                selected = "click[< prev]"
                print(
                    f"Purchase veto (attempt {state.knowledge['skips_used']}/{MAX_SKIP}) -> returning to results and suspending {env.cur_asin} until a new candidate is verified"
                )
        took_intent = bool(selected)
        deadlock_reason = ""
        if not took_intent and (not deferred_intent):
            if same_intent and intent_repeat + 1 >= DEADLOCK_K:
                deadlock_reason = f"The same intent could not be grounded for {intent_repeat + 1} consecutive steps"
            elif optimistic_streak >= DEADLOCK_K:
                deadlock_reason = f"For {optimistic_streak} consecutive steps, assessments were optimistic but the task remained unfinished"
        if deadlock_reason:
            print(f"Deadlock reassessment: {deadlock_reason}")
            rr, _c = re_review_deadlock(
                task,
                env,
                ledger,
                filtered,
                intent or last_intent,
                deadlock_reason,
                history,
                client,
                model,
                max_tokens,
            )
            llm_calls += _c
            if rr:
                selected = rr
                print(f"Reassessment selected: {selected}")
        if took_intent:
            print(f"Intent-direct execution: {selected}")
        if selected is None:
            if env.page_type == PAGE_SEARCH:
                for v in range(0, 3):
                    q = build_query(task, variant=v)
                    if q and (not is_repeat_query(state, q)):
                        selected = f"search[{q}]"
                        break
                if selected is None:
                    selected = f"search[{build_query(task, 0)}]"
                print(
                    f"B: ungrounded intent on search page -> rule-based fallback query: {selected}"
                )
            elif env.page_type != PAGE_RESULTS:
                print(
                    f"B: not on results page and intent ungrounded (intent={intent[:40]}) -> trying to return"
                )
                _low = {a.lower(): a for a in filtered}
                selected = None
                for _pref in ("click[< prev]", "click[<prev]", "click[prev]"):
                    if _pref in _low:
                        selected = _low[_pref]
                        break
                if selected is None:
                    selected = _low.get(
                        "click[back to search]",
                        filtered[0] if filtered else "click[back to search]",
                    )
                print(f"B: return action: {selected}")
            else:
                print(
                    f"A: fallback results-page comparison ({len(env.products)} products on this page)"
                )
                cands, _c = quick_filter_candidates(
                    task,
                    env.products,
                    ledger,
                    TOPK,
                    client,
                    model,
                    max_tokens,
                    think_text,
                    state.knowledge["filter_principles"],
                )
                llm_calls += _c
                if deferred_intent:
                    _a = deferred_intent.strip()[6:-1].lower()
                    cands = [p for p in env.products if p.asin == _a] + [
                        p for p in cands if p.asin != _a
                    ]
                print(
                    f"   Candidates awaiting verification: {len(cands)}: {[p.asin for p in cands]}"
                )
                for prod in cands:
                    page, res, verdict, _c = simulate_action(
                        task,
                        env,
                        prod.asin,
                        prod,
                        client,
                        model,
                        max_tokens,
                        use_probe=use_probe,
                        use_step2=use_step2,
                    )
                    llm_calls += _c
                    if page is None:
                        print(f"   Warning: real probe failed; skipping {prod.asin}")
                        continue
                    ledger.add(page, res, verdict)
                    suspended.clear()
                    print(f"   · {page.asin} ${page.price_low} {page.title[:40]!r}")
                    print(
                        f"       matched={len(res.matched)} missing={res.missing or 'none'}"
                    )
                    if verdict:
                        print(f"       step2: {verdict[:110]}")
                clickable_now = {p.asin for p in env.products}
                asin, _c = select_best_action(
                    task,
                    ledger,
                    skipped,
                    client,
                    model,
                    max_tokens,
                    clickable_now=clickable_now,
                )
                llm_calls += _c
                if asin:
                    selected = f"click[{asin}]"
                    print(f"select chose: {selected}")
                    _v = ledger.verdicts.get(asin, "")
                    is_optimistic = "FULL_MATCH" in _v or "NEAR_MATCH" in _v
                else:
                    selected = next(
                        (a for a in filtered if a.lower() == "click[next >]"),
                        filtered[0] if filtered else "click[back to search]",
                    )
                    print(
                        f"Warning: select found no clickable candidate; falling back to: {selected}"
                    )
            deferred_intent = ""
        if (
            steps_left <= CLOSE_RESERVE
            and ledger.entries
            and (env.page_type != PAGE_END)
        ):
            best = None
            if (
                env.page_type == PAGE_ITEM
                and ledger.has(env.cur_asin)
                and (env.cur_asin not in skipped)
            ):
                best = ledger.entries[env.cur_asin]
            if best is None:
                here = {p.asin for p in env.products}
                best = ledger.best(exclude=skipped, allow=here) or ledger.best(
                    allow=here
                )
            if best is None:
                qs = state.knowledge["queries_issued"]
                if qs and env.page_type != PAGE_RESULTS:
                    print(
                        f"Remaining {steps_left} steps and no clickable verified candidate on this page -> repeating the query to restore the page"
                    )
                    env.step(
                        f"search[{qs[-1]}]"
                        if env.page_type == PAGE_SEARCH
                        else "click[back to search]"
                    )
                    trajectory.append(
                        StepRecord(
                            kind="act", text=" (fallback purchase: restoring the page)"
                        )
                    )
                    here = {p.asin for p in env.products}
                    best = ledger.best(exclude=skipped, allow=here) or ledger.best(
                        allow=here
                    )
            if best is not None:
                bp, br = best
                print(
                    f"Remaining {steps_left} steps -> forcing purchase of the best candidate {bp.asin}"
                )
                if env.cur_asin != bp.asin or env.page_type != PAGE_ITEM:
                    if env.page_type == PAGE_ITEM:
                        env.step("click[< prev]")
                        trajectory.append(StepRecord(kind="act", text="click[< prev]"))
                    if env.page_type == PAGE_RESULTS and bp.asin in {
                        p.asin for p in env.products
                    }:
                        env.step(f"click[{bp.asin}]")
                        trajectory.append(
                            StepRecord(kind="act", text=f"click[{bp.asin}]")
                        )
                if env.page_type == PAGE_ITEM:
                    env.ensure_options(task, trace=trajectory)
                    obs, reward, done = env.step("click[buy now]")
                    trajectory.append(StepRecord(kind="act", text="click[buy now]"))
                    print(f"Observation: {obs[:160]}\nReward: {reward}  Done: {done}")
                    if done:
                        _diag_capture(diag, env, task, obs, reward, "Fallback purchase")
                        print(f"Fallback purchase completed; score: {reward}")
                        print(
                            f"Counted steps: {env.env_steps}  probes: {env.probe_steps}  LLM {llm_calls}"
                        )
                        return (reward, True, trajectory, llm_calls, env.probe_steps)
        if re.match("^click\\[\\s*buy\\s*now\\s*\\]$", (selected or "").strip(), re.I):
            env.ensure_options(task, trace=trajectory)
        print(f"Selected action: {selected}")
        trajectory.append(StepRecord(kind="act", text=selected))
        last_actions.append(selected)
        obs, reward, done = env.step(selected)
        trajectory.append(StepRecord(kind="ob", text=obs))
        print(f"Observation: {obs[:220]}")
        print(f"Reward: {reward}, done: {done}")
        state.observations.append((selected, obs))
        state.knowledge["tried_actions"].add(selected)
        update_stuck_state(stuck_state, last_actions)
        if intent and same_intent and (not took_intent):
            intent_repeat += 1
        else:
            intent_repeat = 0
        last_intent = intent
        optimistic_streak = optimistic_streak + 1 if is_optimistic and (not done) else 0
        if deadlock_reason:
            intent_repeat = optimistic_streak = 0
        history += f"\n> think: {think_text[:200]}\n> {selected}\n{obs[:400]}\n"
        if len(history) > 6400:
            history = history[-6400:]
        if done:
            _diag_capture(diag, env, task, obs, reward, "Normal purchase")
            print(f"Completed; score: {reward}")
            print(
                f"Counted steps: {env.env_steps}  probes: {env.probe_steps}  LLM {llm_calls}"
            )
            return (reward, True, trajectory, llm_calls, env.probe_steps)
    _diag_capture(diag, env, task, "", 0.0, "No purchase / step budget exhausted")
    print(
        f"No purchase or step budget exhausted. Counted steps: {env.env_steps}  probes: {env.probe_steps}  LLM {llm_calls}"
    )
    return (0.0, False, trajectory, llm_calls, env.probe_steps)


DIAG_COLS = [
    ("session", "Session"),
    ("reward", "Reward"),
    ("n_total", "Inferred denominator N"),
    ("n_short", "Missing component count"),
    ("path", "Purchase path"),
    ("miss_kind", "Suspected missing requirements"),
    ("buy_asin", "Purchased ASIN"),
    ("env_options", "Environment options receipt (facts)"),
    ("task_attrs", "Requested attributes"),
    ("price_max", "Budget limit"),
    ("buy_price", "Purchased item lower price"),
    ("fill", "Options added by ensure_options"),
    ("check_matched", "Pre-purchase MATCHED"),
    ("check_missing", "Pre-purchase MISSING"),
    ("check_unknown", "Pre-purchase UNKNOWN"),
    ("check_soft", "Pre-purchase title-only matches"),
    ("buy_title", "Product title"),
    ("task_keywords", "Task keywords"),
    ("steps", "Steps"),
    ("llm", "LLM calls"),
    ("probes", "Probe steps"),
]


def diag_infer_miss(d: Dict[str, Any]) -> str:
    out: List[str] = []
    env_opt = (d.get("env_options") or "").lower()
    sel: Dict[str, str] = {}
    for m in re.finditer("['\"]([^'\"]+)['\"]\\s*:\\s*['\"]([^'\"]*)['\"]", env_opt):
        sel[m.group(1).strip()] = m.group(2).strip()
    attrs = {}
    for part in (d.get("task_attrs") or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            attrs[k.strip().lower()] = v.strip().lower()
    for k, want in attrs.items():
        got = None
        for sk, sv in sel.items():
            if _norm(sk) == _norm(k) or _norm(k) in _norm(sk) or _norm(sk) in _norm(k):
                got = sv
                break
        if got is None:
            out.append(f"{k}not selected")
        elif not value_matches(want, [got])[0]:
            out.append(f"{k}wrong selection ({got})")
    if not sel and attrs:
        out.append("empty options (all selected options lost)")
    pm, bp = (d.get("price_max"), d.get("buy_price"))
    if pm is not None and bp is not None and (bp > pm + 1e-09):
        out.append("price exceeds budget")
    return (
        " | ".join(out)
        or "(attributes and options match; missing components likely concern title/description attributes)"
    )


def write_diagnosis(wb, rows: List[Dict[str, Any]]) -> None:
    ws = wb.create_sheet("Diagnosis")
    for j, (_, label) in enumerate(DIAG_COLS, 1):
        ws.cell(row=1, column=j, value=label)
    for i, d in enumerate(rows, 2):
        d = dict(d)
        d["miss_kind"] = diag_infer_miss(d)
        for j, (key, _) in enumerate(DIAG_COLS, 1):
            v = d.get(key)
            ws.cell(
                row=i,
                column=j,
                value=v if isinstance(v, (int, float)) or v is None else str(v)[:400],
            )
    ws2 = wb.create_sheet("DiagSummary")
    ws2["A1"] = "Task distribution by number of missing components"
    ws2["A2"], ws2["B2"] = ("Missing component count", "Task count")
    from collections import Counter

    cn = Counter((d.get("n_short") for d in rows if d.get("n_short") is not None))
    r = 3
    for k in sorted((x for x in cn if x is not None)):
        ws2.cell(row=r, column=1, value=k)
        ws2.cell(row=r, column=2, value=cn[k])
        r += 1
    r += 1
    ws2.cell(
        row=r,
        column=1,
        value="Attribution of suspected missing requirements (all tasks)",
    )
    r += 1
    ws2.cell(row=r, column=1, value="Missing requirement")
    ws2.cell(row=r, column=2, value="Task count")
    r += 1
    ck = Counter()
    for d in rows:
        for piece in diag_infer_miss(d).split(" | "):
            ck[piece] += 1
    for k, v in ck.most_common():
        ws2.cell(row=r, column=1, value=k)
        ws2.cell(row=r, column=2, value=v)
        r += 1
    r += 1
    ws2.cell(
        row=r,
        column=1,
        value="Missing-requirement attribution for tasks missing exactly one component",
    )
    r += 1
    ws2.cell(row=r, column=1, value="Missing requirement")
    ws2.cell(row=r, column=2, value="Task count")
    r += 1
    c1 = Counter()
    for d in rows:
        if d.get("n_short") == 1:
            for piece in diag_infer_miss(d).split(" | "):
                c1[piece] += 1
    for k, v in c1.most_common():
        ws2.cell(row=r, column=1, value=k)
        ws2.cell(row=r, column=2, value=v)
        r += 1


def write_results(
    scores: List[float],
    out_path: str,
    method: str,
    llm_total: int,
    llm_per: List[int],
    probe_total: int,
    probe_per: List[int],
    steps_per: List[int],
    bought: List[bool],
    diag_rows: Optional[List[Dict[str, Any]]] = None,
) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Results"
    n = len(scores)
    succ = sum((1 for s in scores if s >= 1.0))
    nb = sum((1 for b in bought if b))
    ws["A1"], ws["B1"] = ("Method", method)
    ws["A2"], ws["B2"] = ("Total Episodes", n)
    ws["A3"], ws["B3"] = ("Avg Score", f"{sum(scores) / max(1, n):.4f}")
    ws["A4"], ws["B4"] = (
        "Success Rate (==1.0)",
        f"{succ}/{n} = {succ / max(1, n):.2%}",
    )
    ws["A5"], ws["B5"] = ("Purchase Rate", f"{nb}/{max(1, n)} = {nb / max(1, n):.2%}")
    ws["A6"], ws["B6"] = ("Total LLM Calls", llm_total)
    ws["A7"], ws["B7"] = ("Avg LLM Calls/Task", f"{llm_total / max(1, n):.2f}")
    ws["A8"], ws["B8"] = ("Total Probe Steps (excluded from max_steps)", probe_total)
    ws["A9"], ws["B9"] = (
        "Avg Counted Steps/Task",
        f"{sum(steps_per) / max(1, len(steps_per)):.1f}",
    )
    ws["A11"], ws["B11"], ws["C11"], ws["D11"], ws["E11"] = (
        "Session",
        "Score",
        "LLM Calls",
        "Probe Steps",
        "Steps",
    )
    for i, s in enumerate(scores):
        ws[f"A{12 + i}"] = i
        ws[f"B{12 + i}"] = round(s, 4)
        if i < len(llm_per):
            ws[f"C{12 + i}"] = llm_per[i]
        if i < len(probe_per):
            ws[f"D{12 + i}"] = probe_per[i]
        if i < len(steps_per):
            ws[f"E{12 + i}"] = steps_per[i]
    if diag_rows:
        write_diagnosis(wb, diag_rows)
    wb.save(out_path)
    print(f"Results saved to: {out_path}")


def main(mode: Optional[str] = None, out_path: Optional[str] = None):
    mode = (mode or os.getenv("WSPRO_MODE", "PRO")).strip().upper()
    if mode not in MODE_DESC:
        raise ValueError(
            f"mode must be one of {'/'.join(MODE_DESC)}; received {mode!r}"
        )
    from web_agent_site.envs import WebAgentTextEnv

    _seed_env = os.getenv("WSPRO_SEED", "233").strip().lower()
    if _seed_env in ("off", "none", "random", "-1", ""):
        print(
            "[Random tasks] No fixed seed: product prices and task budgets change across runs. Use only for comparison with historical runs; use the default fixed-seed mode for new experiments."
        )
    else:
        import random as _stdrandom

        _WS_SEED = int(_seed_env)
        _stdrandom.seed(_WS_SEED)
        try:
            import numpy as _np

            _np.random.seed(_WS_SEED)
        except Exception:
            pass
        print(
            f"[Fixed tasks] random.seed({_WS_SEED}) set before environment construction for reproducible product prices and task budgets"
        )
    num_products = os.getenv("WEBSHOP_NUM_PRODUCTS")
    raw_env = WebAgentTextEnv(
        observation_mode="text",
        num_products=int(num_products) if num_products else None,
    )
    env = WebShopPro(raw_env)
    client = build_client()
    model = os.getenv("OPENAI_MODEL", "qwen3-max-2026-01-23")
    max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))
    num_episodes = int(os.getenv("WEBSHOP_NUM_EPISODES", "500"))
    max_steps = int(os.getenv("WEBSHOP_MAX_STEPS", "50"))
    print("=" * 66)
    print(f"[{VERSION}] {VERSION_DESC}")
    print(f"[{mode}] {MODE_DESC[mode]}")
    print(
        f"Model: {model} | sessions: {num_episodes} | max_steps: {max_steps} | topk: {TOPK}"
    )
    print(
        "Architecture: think -> intent-direct execution -> fallback step1+step2 -> select, with WebShop-specific knowledge at each stage; decisions are made by the LLM."
    )
    print("=" * 66)
    scores: List[float] = []
    llm_total = probe_total = 0
    llm_per: List[int] = []
    probe_per: List[int] = []
    steps_per: List[int] = []
    bought: List[bool] = []
    diag_rows: List[Dict[str, Any]] = []
    out_file = out_path or f"webshop_pro_results_{VERSION}_{mode}_{num_episodes}.xlsx"
    for idx in range(num_episodes):
        print(f"\n{'=' * 66}")
        print(f"[{mode}] Task {idx + 1}/{num_episodes}: webshop/session_{idx}")
        print(f"{'=' * 66}")
        env.reset(session=idx)
        diag: Dict[str, Any] = {"session": idx}
        r, done, traj, calls, probes = run_episode(
            env,
            client=client,
            model=model,
            max_tokens=max_tokens,
            max_steps=max_steps,
            mode=mode,
            diag=diag,
        )
        scores.append(float(r))
        llm_total += calls
        llm_per.append(calls)
        probe_total += probes
        probe_per.append(probes)
        steps_per.append(env.env_steps)
        bought.append(
            any((t.kind == "act" and "buy now" in t.text.lower() for t in traj))
        )
        diag["steps"], diag["llm"], diag["probes"] = (env.env_steps, calls, probes)
        diag_rows.append(diag)
        print(
            f"[Diagnostic] reward={diag.get('reward')} missing {diag.get('n_short')} / total {diag.get('n_total')} components | missing requirements: {diag_infer_miss(diag)}"
        )
        print(
            f"[Diagnostic] Environment options receipt: {diag.get('env_options') or '(empty)'}"
        )
        try:
            write_results(
                scores,
                out_file,
                method=f"[{VERSION}] {VERSION_DESC} | {MODE_DESC[mode]} [{mode}]",
                llm_total=llm_total,
                llm_per=llm_per,
                probe_total=probe_total,
                probe_per=probe_per,
                steps_per=steps_per,
                bought=bought,
                diag_rows=diag_rows,
            )
        except Exception as e:
            print(
                f"Warning: incremental result saving failed (continuing): {str(e)[:80]}"
            )
        succ = sum((1 for s in scores if s >= 1.0))
        nb = sum((1 for b in bought if b))
        print(f"\nCurrent progress: {idx + 1}/{num_episodes}  [{mode}]")
        print(
            f"  score={r:.4f}  avg={sum(scores) / len(scores):.4f}  success={succ}/{len(scores)}"
        )
        print(
            f"  mean steps={sum(steps_per) / len(steps_per):.1f}  purchase rate={nb}/{len(bought)}"
        )
        print(
            f"  total LLM calls={llm_total} (mean {llm_total / len(scores):.1f}/task)  total probes={probe_total}"
        )
    write_results(
        scores,
        out_file,
        method=f"[{VERSION}] {VERSION_DESC} | {MODE_DESC[mode]} [{mode}]",
        llm_total=llm_total,
        llm_per=llm_per,
        probe_total=probe_total,
        probe_per=probe_per,
        steps_per=steps_per,
        bought=bought,
        diag_rows=diag_rows,
    )


if __name__ == "__main__":
    main()
