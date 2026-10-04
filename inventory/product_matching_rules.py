"""What article a product to classify is offered in the « À classer » panel.

Three sources, asked in this order (`suggest_for_product`), each saying on
screen which it is (`ai_suggestion["source"]`) and why:

1. **neighbour** - a product the owner has ALREADY classified whose name says
   the same thing (`ClassifiedNeighbours`): the same words once sizes, pack
   counts and house-brand prefixes are set aside, and - for an article
   counted by the unit - the same sizes. Its article is offered, and its
   conversion factor copied when the two names are the same pack. Learned
   from the owner's own classifications, so it follows their conventions
   (« Vodka » for every bottle size, but a 25cl cup and a 50cl cup two
   articles) rather than anybody's guess about how a bar files things.
   A suggestion is made against the classifications as they stand
   (`classified_fingerprint`) and made again once they move.
2. **rule** - the hand-written table below (a spirit, a syrup, a deposit).
3. **fallback** - the raw name with its sizes stripped, under the category
   the word classifier learned from the classified products.

Hardcoded product-name -> stock item rules, replacing an earlier
Ollama-based naming suggestion (removed - see git history if it's ever worth
revisiting with a faster/more reliable model). Regex over an LLM call for
this specific job because:

- It's instant. No 20-60s/batch wait, no Ollama process to keep warm.
- It's deterministic. The exact same input always produces the exact same
  output - no risk of the batch-homogeneity or cross-product mixups seen in
  testing (an entire batch of similar MPRO cleaning products all coming back
  named "Matériel" - the category, not an item; two different syrup flavours
  both named "Sirop Fraise").
- It's auditable. Every rule here is something a human deliberately decided,
  not a guess a model made once and might make differently next time.

The tradeoff is precision, not coverage: unlike a model, this can't
generalise to a product it's never seen a pattern for - anything that
doesn't match any rule below still gets a suggestion (see
apply_rules_to_pending_products), just a low-effort one built from the raw
invoice name instead of a deliberately-named rule. The goal is that every
product in the review queue arrives pre-filled and internally consistent,
never blank; sorting out which of those raw-name guesses should really be
merged into one stock item is a manual pass for later (see
StockTypeUpdateView's merge prompt), not something this module tries to get
right up front.

Rule order matters: more specific patterns (a flavoured syrup, a bourbon)
are listed before the generic spirit/category keyword they'd otherwise also
match (a bare "RHUM"), since the first matching rule wins.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import cast

from rapidfuzz.distance import Levenshtein

from .matching import numeric_signature
from .models import StockType, UnitChoices
from .quantity_extraction import extract_quantity_for_product, size_signature, strip_size_and_count_tokens


def _normalize_casing(text: str) -> str:
    """First letter capitalized, everything else lowercase."""
    text = text.strip()
    return text[:1].upper() + text[1:].lower() if text else text


# A handful of raw names carry a supplier marker with no meaning of its own
# (Metro prefixes some lines with "*") - stripped before a name is used
# as-is for the no-rule-matched fallback below, so it doesn't leak into a
# stock item name as literal punctuation.
_LEADING_JUNK_RE = re.compile(r"^[*\s]+")

# Same "small dose, not a whole bottle" exception as the dedicated Bitter
# rule in _BEER_RULES, but usable for the fallback path too - a bitters
# variant with no other rule match should still stay volume-tracked rather
# than falling into the generic small-format "just count bottles" shortcut.
_BITTER_LIKE_RE = re.compile(r"\bBITTER\b")

# No category has ever been recognised for this product - matches the user's
# own convention (found by auditing their real classifications) rather than
# an invented label like "Autre" that never actually appears in their data.
_UNKNOWN_CATEGORY = "Inconnu"

# Common French function words - generic linguistic noise, not product
# vocabulary, so they're excluded from category guessing below regardless
# of which category happens to contain them.
_CATEGORY_STOPWORDS = {
    "DE",
    "DU",
    "DES",
    "LA",
    "LE",
    "LES",
    "ET",
    "EN",
    "AU",
    "AUX",
    "A",
    "AVEC",
    "SANS",
    "POUR",
    "SUR",
    "UN",
    "UNE",
    "SA",
}


def _category_words(raw_name: str) -> set[str]:
    cleaned = strip_size_and_count_tokens(raw_name)
    return {w for w in re.findall(r"[A-ZÀ-ÖØ-Þ]+", cleaned) if len(w) >= 3 and w not in _CATEGORY_STOPWORDS}


def build_category_classifier() -> dict[str, Counter]:
    """Learns which words tend to appear in which category from every
    product the user has already classified, so a product no rule
    recognises can still get a sensible category guess instead of always
    landing in the same catch-all bucket. Deliberately not a hardcoded
    keyword table: it's derived fresh from the user's own real
    classifications every time this runs (cheap at this scale), so it
    reflects whatever conventions they actually use rather than a guess
    about what a bar's categories generically look like.
    """
    from .models import Product

    category_words: dict[str, Counter] = defaultdict(Counter)
    products = Product.objects.filter(stock_type__isnull=False, stock_type__category__gt="").select_related(
        "stock_type"
    )
    for product in products:
        category = product.stock_type.category
        for word in _category_words(product.raw_name):
            category_words[category][word] += 1
    return category_words


def guess_category(raw_name: str, category_words: dict[str, Counter]) -> str | None:
    """Scores every category by how much its known vocabulary overlaps with
    this product's name, weighting each shared word by how exclusively it
    belongs to that category (a word split evenly across categories carries
    no signal; one that's only ever appeared in "Spiritueux" carries a lot).
    Returns None - not a guess - when nothing in the name has ever been seen
    before, or the signal is too weak/ambiguous to trust.
    """
    words = _category_words(raw_name)
    if not words or not category_words:
        return None

    scores: Counter[str] = Counter()
    for word in words:
        total = sum(counts.get(word, 0) for counts in category_words.values())
        if not total:
            continue
        for category, counts in category_words.items():
            if counts.get(word):
                scores[category] += counts[word] / total

    if not scores:
        return None
    ranked = scores.most_common(2)
    best_category, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0
    # Require both a minimum absolute signal and a clear lead over whatever
    # came second - otherwise a product split evenly between two categories
    # would get an arbitrary tie-break instead of an honest "don't know".
    # Tuned by leave-one-out validation against every product in this
    # database that's already been manually classified (260 that don't
    # match a named rule): this threshold is right ~60% of the time it
    # guesses, wrong ~5%, and honestly abstains the rest - a looser
    # threshold answers more often but wrong noticeably more often too.
    if best_score >= 0.9 and best_score >= runner_up_score * 1.5:
        return best_category
    return None


def _resolve_stock_type_match(suggestion: dict) -> None:
    """Fills in is_new_stock_type/matched_stock_type_id from an authoritative
    lookup, so the review template can trust them without re-doing the
    lookup itself. An existing article keeps its own spelling in the
    suggestion (« Cola Testbrand VC », not « Cola testbrand vc »): the panel's field is
    pre-filled with it, and what the person sees is what will be used."""
    suggestion["stock_type_name"] = _normalize_casing(suggestion.get("stock_type_name") or "")
    suggestion["new_stock_type_category"] = _normalize_casing(suggestion.get("new_stock_type_category") or "")
    name = suggestion["stock_type_name"]
    match = StockType.objects.filter(name__iexact=name).first() if name else None
    suggestion["is_new_stock_type"] = match is None
    suggestion["matched_stock_type_id"] = match.id if match else None
    if match is not None:
        suggestion["stock_type_name"] = match.name
    # Pre-format so the review form's editable input doesn't show something
    # like "0.7000000000000001", and so a Decimal never ends up in a dict
    # that's about to be saved into a JSONField (json.dumps doesn't know how
    # to serialize one). Four decimals at most, what stock_equivalent holds:
    # « SAFRAN 0,25G » is 0.00025 kg, which « Approuver » refuses, and the
    # suggestion made again was the same.
    exact = suggestion.get("stock_equivalent", 1)
    try:
        factor = round(float(exact), 4)
        exact_dec = Decimal(str(exact))
    except (TypeError, ValueError, InvalidOperation):
        factor, exact_dec = 1, Decimal(1)
    suggestion["stock_equivalent"] = f"{factor:g}"
    if factor == 0 or abs(Decimal(str(factor)) - exact_dec) > abs(exact_dec) * Decimal("0.005"):
        # 0.04 g is 0.00004 kg, which four decimals make 0: no factor at all,
        # left to a person - « Approuver » refused the 0, and the suggestion
        # made again was the same, at every click. 0.25 g made 0.0003, still
        # sure, and « Approuver les sûres » booked every purchase 20 % over:
        # a factor the rounding moves by more than 0.5 % (1/3, 0.3333, is not)
        # is no factor either.
        suggestion["stock_equivalent"] = ""
        suggestion["confidence"] = "low"
        suggestion["reasoning"] += (
            f" 1 produit = {_quantity_display(exact_dec)} : plus fin que les 4 décimales d'un facteur, à saisir."
        )


# --- Confidence -------------------------------------------------------------
#
# A suggestion's confidence is about the whole of it - the article AND the
# conversion factor - since « Approuver les N sûres » books stock movements
# from both. It is the LEAST confident of the two: a right article at a
# wrong factor is silently wrong money, the failure this codebase keeps
# meeting. Each source's article confidence below was measured by
# leave-one-out over the owner's classified products (scratchpad
# loo_pipeline.py, the index rebuilt WITHOUT the product being judged -
# merely skipped in `find`, it went on teaching `tells_apart` that its own
# extra word was a difference, and hid the wrong loose matches), and "high"
# is reserved for what that benchmark showed right on article, category AND
# factor for every product it named: a few per cent of the classified
# products. Nothing wider passed - see `NeighbourMatch.article_confidence`
# and `_neighbour_suggestion`. The figures themselves stay in the scratchpad
# (loo_pipeline_out.txt): the repository is public, and a count of the
# owner's products is the owner's business.
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def least_confident(*levels: str) -> str:
    """The lowest of several confidence levels; an unknown level counts as
    "low" - nothing unmeasured is ever approved in bulk."""
    if not levels or any(level not in _CONFIDENCE_ORDER for level in levels):
        return "low"
    return min(levels, key=_CONFIDENCE_ORDER.__getitem__)


# What a rule or a fallback may claim about the ARTICLE it names. A rule
# naming an article that exists is right about half the time on the
# benchmark (the owner's articles are finer than the table: a keg and a
# bottle of one beer are two articles where the rule names one); one naming a new article, and
# a raw name proposed as a new article, cannot be checked at all - a new
# article is a name nobody has typed yet.
RULE_ARTICLE_CONFIDENCE = "medium"
NEW_ARTICLE_CONFIDENCE = "medium"
FALLBACK_EXISTING_ARTICLE_CONFIDENCE = "medium"


@dataclass(frozen=True)
class MatchRule:
    pattern: re.Pattern
    stock_type_name: str
    category: str
    unit: str = UnitChoices.UNIT
    # Overrides for quantity_extraction, for a whole category of product
    # where its general logic (parse a size/count from the name, shortcut
    # small formats to "1 unit") is known not to apply - see
    # extract_quantity()'s own docstring for what each one means.
    force_unit_count: bool = False
    assume_volume_tracked: bool = False


def _rule(
    pattern: str,
    stock_type_name: str,
    category: str,
    unit: str = UnitChoices.UNIT,
    force_unit_count: bool = False,
    assume_volume_tracked: bool = False,
) -> MatchRule:
    return MatchRule(re.compile(pattern), stock_type_name, category, unit, force_unit_count, assume_volume_tracked)


# --- Deposits (crates, kegs, jugs - PLEIN=charge, VIDE=refund; see
# invoices/parsers/metro.py for how these end up with a negative
# quantity/total on the refund line). Always exactly 1 - a crate is a
# single returnable object regardless of what it holds or what number the
# name happens to mention (a crate marked "24X33CL" describes the crate's
# CONTENTS; the deposit is charged per crate, not per bottle inside it).
_DEPOSIT_RULES = [
    _rule(r"CAISSE\s*COCA", "Casier Coca", "Consignes", force_unit_count=True),
    _rule(r"CAIS\.?\s*PERRIER", "Casier Perrier", "Consignes", force_unit_count=True),
    _rule(r"CAIS\.?\s*PERSON", "Casier verre", "Consignes", force_unit_count=True),
    _rule(r"\bFUT\b.*FELSGOLD|FELSGOLD.*\bFUT\b", "Fût Felsgold", "Consignes", force_unit_count=True),
    _rule(r"\bSTUB\b.*EVIAN|EVIAN.*\bSTUB\b", "Bonbonne Evian", "Consignes", force_unit_count=True),
    _rule(r"PALETTE\s*EUROPE", "Palette Livraison", "Livraison", force_unit_count=True),
]

# --- Cleaning / consumables (MPRO = Metro's own "Metro Pro" house brand -
# the number right after it is a pack count, handled by quantity_extraction,
# not part of the name) --------------------------------------------------
_CONSUMABLES_RULES = [
    _rule(r"GANT.*LATEX|LATEX.*GANT", "Gants latex", "Consommables"),
    _rule(r"CUILLERE", "Cuillères café", "Consommables"),
    _rule(r"\bSERV\b(?!ICE)", "Serviettes", "Consommables"),
    _rule(r"\bPAILLE", "Pailles", "Consommables"),
    _rule(r"FEUTRE.*CRAIE", "Feutres craie", "Consommables"),
    _rule(r"BOULE.*INOX", "Boule inox", "Matériel"),
    _rule(r"LAVE.?VITRE", "Lave-vitre", "Consommables", UnitChoices.LITRE),
    _rule(r"LIQ.*VAISS|VAISSELLE", "Liquide vaisselle", "Consommables", UnitChoices.LITRE),
    _rule(r"RINCAGE.*MACHINE|MACHINE.*RINCAGE", "Liquide rinçage machine", "Consommables", UnitChoices.LITRE),
    _rule(r"\bJAVEL\b", "Nettoyant javel", "Consommables"),
    _rule(r"NETT.*SANITAIRE|SANITAIRE.*NETT", "Nettoyant sanitaire", "Consommables"),
    _rule(r"SAC.*POUB|POUBELLE", "Sac Poubelle", "Consommables"),
    _rule(r"\bSPATULE\b", "Spatule", "Matériel"),
    _rule(r"^VAP\s", "Verre à Pied", "Matériel"),
    _rule(r"\bETIQ(UETTE)?S?\b", "Étiquettes", "Consommables"),
]

# --- Flavoured syrups / juices / purées - checked BEFORE the generic
# spirit rules below, since e.g. a rum-FLAVOURED syrup ("SIROP ... RHUM")
# would otherwise wrongly match the actual Rhum spirit rule. Brand (Monin,
# Gilbert, Rioba - Metro's own house brand for juices/syrups) is always
# dropped so every brand of the same flavour groups together. -----------
_FLAVOURED_DRINK_RULES = [
    # "LE FRUIT DE MONIN <flavour>" is Monin's fruit purée line specifically
    # (distinct from their syrup line) - captures whatever flavour follows so
    # a flavour not seen yet still gets a sensible, consistent name instead
    # of falling through to manual review only because the exact word wasn't
    # hardcoded.
    _rule(r"LE\s*FRUIT\s*DE\s*MONIN\s+(\w+)", r"Purée \1", "Soft"),
    _rule(r"SIROP.*BASILIC|BASILIC.*SIROP", "Sirop basilic", "Soft"),
    _rule(r"SIROP.*CITRON|CITRON.*SIROP", "Sirop citron", "Soft"),
    _rule(r"SIROP.*GRENAD|GRENADINE", "Sirop grenadine", "Soft"),
    _rule(r"SIROP.*ROSE", "Sirop rose", "Soft"),
    _rule(r"SIROP.*KIWI", "Sirop kiwi", "Soft"),
    _rule(r"SIROP.*RHUM|RHUM.*SIROP", "Sirop rhum", "Soft"),  # a flavouring syrup, not the spirit
    _rule(r"SIROP.*ORGEAT|ORGEAT", "Sirop Orgeat", "Soft"),
    _rule(r"SIROP.*VIOLET", "Sirop Violet", "Soft"),
    _rule(r"GINGER\s*BEER", "Ginger Beer", "Soft"),
    _rule(r"TONIC", "Tonic", "Soft"),
    _rule(r"SAN\s*PELL|PELLEGRINO", "Eau Pétillante", "Soft"),
    _rule(r"NECTAR.*CRANBERRY", "Nectar Cranberry", "Soft"),
    _rule(r"PAMPLEMOUSSE", "Jus de Pamplemousse", "Soft"),
    _rule(r"ANANAS", "Jus d'Ananas", "Soft"),
    _rule(r"\bPOMME\b", "Jus de Pomme", "Soft"),
    _rule(r"ORANGE", "Jus d'Orange", "Soft"),
    _rule(r"LAIT.*COCO|COCO.*LAIT", "Lait de Coco", "Soft"),
    _rule(r"\bSPRITZ\b", "Spritz", "Spiritueux"),
]

# --- Spirits - brand always dropped, so every bottle of the same spirit
# groups together (Sobieski/Wyborowa/Fjorowka -> Vodka, Bellevoye/Jack
# Daniel's/Monkey Shoulder/Nikka -> Whisky, ...). A size (70CL, 1L) still
# gets picked up separately by quantity_extraction - this table is naming
# only. -------------------------------------------------------------------
_SPIRIT_RULES = [
    _rule(r"\bBOURBON\b", "Bourbon", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bVODKA\b|\bVDK\b", "Vodka", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bGIN\b", "Gin", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bRHUM\b", "Rhum", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bWHISKY\b|\bWH\b", "Whisky", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bTEQ(UILA)?\b", "Tequila", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bMEZCAL\b", "Mezcal", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bCACHACA\b", "Cachaça", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bPISCO\b", "Pisco", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bCOINTREAU\b", "Cointreau", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bCACAO\b", "Liqueur Cacao", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bLIMONCEL(LO)?\b", "Limoncel", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bCINZANO\b|\bVERMOUTH\b", "Vermouth", "Spiritueux", UnitChoices.LITRE),
    _rule(r"\bPROS(ECCO)?\b|CONEGLIANO", "Prosecco", "Spiritueux", UnitChoices.LITRE),
]

# --- Beer -----------------------------------------------------------------
_BEER_RULES = [
    _rule(r"\bBLD\b.*0[,.]0D|0[,.]0D.*\bBLD\b", "Bière Sans Alcool", "Bieres", UnitChoices.LITRE),
    _rule(r"\bBLD\b", "Bière Blonde", "Bieres", UnitChoices.LITRE),
    _rule(r"\bBROOKLYN\b|\bCORONA\b", "Bière Du Moment", "Bieres", UnitChoices.LITRE),
    # Bitters bottles are small (10-20cl), which would otherwise trip the
    # small-format "just count bottles" shortcut - but a bitters bottle is
    # poured a dash at a time across hundreds of cocktails, never served
    # whole, so it needs to stay volume-tracked like a full-size spirit.
    _rule(r"\bBITTER\b", "Bitter", "Spiritueux", UnitChoices.LITRE, assume_volume_tracked=True),
]

# --- Food / grocery --------------------------------------------------------
_FOOD_RULES = [
    _rule(r"\bCOMTE\b", "Comté", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"TOMME.*SAVOIE", "Tomme de Savoie", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"TOMME.*GRISE", "Tomme grise", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"CREAM\s*CHEESE", "Cream cheese", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"\bFOIE\s*GRAS\b|\bBLOC\s*FG\b", "Foie Gras", "Consommables", UnitChoices.KILOGRAM),
    _rule(r"\bJB\b.*\bCRU\b|JAMBON.*CRU", "Jambon Cru", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"\bJB\b.*\bCUIT\b|JAMBON.*CUIT", "Jambon cuit", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"^JB\b", "Jambon", "Epicerie", UnitChoices.KILOGRAM),  # fallback for other JB SUP/... variants
    _rule(r"\bRILLETTES\b", "Rillettes", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"SAUCISSE.*SECHE", "Saucisse sèche", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"SAUMON.*FUME", "Saumon fumé", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"TRUITE.*FUME", "Truite fumée", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"POITRINE.*FUME", "Poitrine fumée", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"VIANDE.*GRISON", "Viande des Grisons", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"PATE.*CAMPAGNE", "Pâté de campagne", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"OLIVE\s*VERTE", "Olive verte", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"CITRON\s*CONFIT", "Citron confit", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"\bLIME\b", "Citron Vert", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"PAPRIKA.*FUME|FUME.*PAPRIKA", "Paprika fumé", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"POIVRE.*NOIR|NOIR.*POIVRE", "Poivre noir", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"PIMENT.*ANTILLAIS", "Piment antillais", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"PIMENT.*OISEAU", "Piment oiseau", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"\bCACAHUETE\b", "Cacahuètes", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"\bCHIPS\b", "Chips", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"HARICOTS?\s*BLC", "Haricots blancs", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"\bHO\b.*VIERGE|HUILE.*OLIVE", "Huile d'olive", "Epicerie", UnitChoices.LITRE),
    _rule(r"\bTAHINA\b", "Tahina", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"SCE.*PIQUANTE|SAUCE.*PIQUANTE", "Sauce piquante", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"SUCRE.*PDR|SUCRE.*POUDRE", "Sucre en poudre", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"CORNICHON", "Cornichons", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"TOAST.*BRIOCHE", "Toast brioche", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"TOAST.*SEIGLE", "Toast de seigle", "Epicerie", UnitChoices.KILOGRAM),
    _rule(r"VINAIGRE.*BLANC", "Vinaigre Ménager", "Consommables", UnitChoices.LITRE),
    _rule(r"YAOURT.*GREC", "Yaourt Grec", "Epicerie", UnitChoices.KILOGRAM),
]

# Checked in this order - see the module docstring on why specificity order
# matters (a flavoured syrup before the spirit it's flavoured like, etc).
# SPIRIT/BEER are checked before CONSUMABLES specifically because a spirit's
# own tasting notes can read like a consumable keyword ("CACHACA ... PAILLE
# ..." - "paille" is a straw-colour tasting note here, not a drinking straw)
# - naming a whole bottle of spirit after a 0.02€ disposable is a much worse
# mistake than the reverse, so spirits get first look.
RULES: list[MatchRule] = [
    *_DEPOSIT_RULES,
    *_FLAVOURED_DRINK_RULES,
    *_BEER_RULES,
    *_SPIRIT_RULES,
    *_CONSUMABLES_RULES,
    *_FOOD_RULES,
]


def match_stock_type(raw_name: str) -> MatchRule | None:
    name = raw_name.upper()
    for rule in RULES:
        m = rule.pattern.search(name)
        if not m:
            continue
        if m.groups():
            # dynamic capture (e.g. stock_type_name=r"Purée \1") - fill in
            # the captured word. Casing is normalized by the caller (same
            # helper used for every other name here), so no need to do it
            # twice - this can stay whatever case the raw name was in.
            return MatchRule(
                rule.pattern,
                m.expand(rule.stock_type_name),
                rule.category,
                rule.unit,
                rule.force_unit_count,
                rule.assume_volume_tracked,
            )
        return rule
    return None


# --- What the owner already classified ---------------------------------------
#
# The neighbour source. A product is "the same thing" as a classified one
# when their names say the same words once everything that describes HOW it
# was bought is set aside - sizes, pack counts, dimensions, a house-brand
# prefix (what `strip_size_and_count_tokens` already strips for the fallback
# name) and the function words. Deliberately a comparison of two SETS with
# nothing one-sided allowed, rather than a similarity score that rewards
# what is shared: measured against the owner's own classifications, every
# word a name has and its neighbour lacks was a different product -
# a flavour (« FRAISE » / « PASSION »), « CONSIGNE » in front of the goods,
# « ZERO », « LIGHT », « PET » against « VC » - and a short name contained
# in a long one (« LOUCHE » in « 12 LOUCHE INOX TABLE ») scored 100 on a
# token-set ratio while naming another article. A name says what it says; a
# word missing on one side is not noise.
#
# Two things ARE forgiven for the MATCH, each a way one supplier prints the
# same word: an abbreviation (« TIMB » for « TIMBALE », « PAP » for
# « PAPIER », a plural) and, on a long word, one typo (« PROSECO »). Neither
# is forgiven for a SURE suggestion (`NeighbourMatch.words_sure`): a prefix
# is a guess about what a supplier's shortened word stands for, and the
# owner's own vocabulary is full of words that are prefixes of other words
# filed elsewhere (« VIN » / « VINAIGRE », « SEL » / « SELECTION », « SAC » /
# « SACHET » - the shape, not the owner's words). A plural is the same word.
# And the guess is LEARNED, the way `tells_apart` is: once the classified
# names print both spellings, each under articles the other never appears
# under, they are two words and the pairing is refused
# (`ClassifiedNeighbours.two_words`, applied through `alike`); a short word
# the owner only ever prints short (« GOB ») goes on standing for the long
# one. Numbers are compared apart, twice: the SIZES
# (`quantity_extraction.size_signature`) decide whether two names are the
# same thing for an article counted by the unit - the owner files a 25cl cup
# and a 50cl cup as two articles but every bottle size of a spirit as one, so
# for an article tracked by volume or weight the size goes into the
# conversion factor instead - and EVERY number (`matching.numeric_signature`)
# decides whether the two are the same pack, which is what allows the
# neighbour's conversion factor to be copied - unless the product's own
# invoice line says otherwise (`_neighbour_suggestion`).

_WORD_RE = re.compile(r"[A-Z0-9]+")
# « SANS » and « AVEC » are function words to the category classifier but
# not here: « LIMONADE SANS BULLES » is not « LIMONADE BULLES ».
_NEIGHBOUR_STOPWORDS = _CATEGORY_STOPWORDS - {"SANS", "AVEC"}
# The shorter of two words must be at least this long to count as an
# abbreviation of the longer one (« GOB » / « GOBELET »; a two-letter « JB »
# stands for nothing on its own).
ABBREVIATION_MIN_LENGTH = 3
# A word this long may carry one typo and still be the same word
# (« PROSECO » / « PROSECCO »). Below it, one letter is a different word:
# « BRUN » and « BRUT », « SEC » and « SEL ».
TYPO_MIN_LENGTH = 7


def fold_name(name: str) -> str:
    """Upper-case ASCII: « Crème » and « CREME » are one word."""
    decomposed = unicodedata.normalize("NFKD", name.upper())
    return "".join(char for char in decomposed if char.isascii())


def same_word(a: str, b: str) -> bool:
    """Two printed words for one word: equal, one an abbreviation (or the
    singular) of the other, or - long enough - one typo apart."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if len(short) >= ABBREVIATION_MIN_LENGTH and long.startswith(short):
        return True
    # Letters only: a code one character off (« REF7001 » / « 1REF7001 »,
    # « 9OZ » / « 7OZ ») is another code, never a typo.
    return len(short) >= TYPO_MIN_LENGTH and a.isalpha() and b.isalpha() and Levenshtein.distance(a, b) == 1


def exact_or_plural(a: str, b: str) -> bool:
    """The same word beyond doubt: spelled the same, or one the plural of the
    other (« SACS » / « SAC »). What a sure suggestion may rest on, where an
    abbreviation or a typo is only good enough for the match."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= ABBREVIATION_MIN_LENGTH and long == short + "S"


def same_words(a: frozenset[str], b: frozenset[str], alike=same_word) -> bool:
    """The two names say the same words: as many on each side, each paired
    with its counterpart on the other and nothing left over. A pairing, not
    two lookups - « CONS. » abbreviates « CONSIGNE », and looked up both ways
    it stood for « CONSIGNE » AND for itself, so « BOUTEILLE GAZ 10KG CONS. »
    matched « Consigne BOUTEILLE GAZ 10KG CONS. », the goods matched their
    deposit. Empty against anything is False: a name with no word left says
    nothing. `alike` is what makes two printed words one (`same_word`, or
    `exact_or_plural` for the stricter question)."""
    if not a or not b or len(a) != len(b):
        return False
    return _pairs_up(sorted(a), list(b), alike)


def _pairs_up(remaining: list[str], available: list[str], alike=same_word) -> bool:
    """Whether every word of `remaining` can take a different counterpart
    from `available` - a perfect matching, by backtracking (names have a
    handful of words)."""
    if not remaining:
        return True
    word, rest = remaining[0], remaining[1:]
    for index, other in enumerate(available):
        if alike(word, other) and _pairs_up(rest, available[:index] + available[index + 1 :], alike):
            return True
    return False


def one_word_apart(a: frozenset[str], b: frozenset[str], alike=same_word) -> tuple[str, str] | None:
    """When one name says exactly one word more than the other and every
    other word pairs up (by `alike`): (which side has it - "a" or "b", the
    word). None otherwise, and None when the shorter name is empty.
    « SIROP TESTFRUIT BIO » against « SIROP TESTFRUIT » is ("a", "BIO").

    This is the LOOSE tier, never more than medium. Measured strictly (the
    judged product out of the index): it names the right article about nine
    times in ten, and the right factor with it somewhat less often, where
    the rules name the right article one time in two. What it gets wrong is
    the FIRST variant of something already filed: the zero-sugar twin of a
    cola, the deposit beside its goods, a brand in front of a keg - a word
    nothing has yet taught `tells_apart`. Once the owner files that twin,
    the word is learned and the next one is refused. When the shorter name
    has ONE word (« LOUCHE » against « LOUCHE INOX »), that word is the whole
    of what the two have in common and the match is right less often -
    about three times in four (scratchpad fix2_measure.py) - so `find`
    keeps it and `NeighbourMatch.article_confidence` rates it low."""
    if not a or not b or abs(len(a) - len(b)) != 1:
        return None
    shorter, longer, side = (a, b, "b") if len(a) < len(b) else (b, a, "a")
    for extra in sorted(longer):
        if _pairs_up(sorted(shorter), [word for word in longer if word != extra], alike):
            return side, extra
    return None


@dataclass(frozen=True)
class NameShape:
    """A product name reduced to what it says (`words`), what sizes it prints
    (`sizes`, from `size_signature`) and every number it carries
    (`numbers`, from `numeric_signature`)."""

    words: frozenset[str]
    sizes: tuple
    numbers: tuple

    @classmethod
    def of(cls, raw_name: str) -> NameShape:
        cleaned = strip_size_and_count_tokens(_LEADING_JUNK_RE.sub("", raw_name))
        words = frozenset(
            word
            for word in _WORD_RE.findall(fold_name(cleaned))
            if not word.isdigit() and word not in _NEIGHBOUR_STOPWORDS
        )
        sizes = tuple(sorted(size_signature(raw_name).items(), key=repr))
        numbers = tuple(sorted(numeric_signature(raw_name).items(), key=repr))
        return cls(words, sizes, numbers)


@dataclass(frozen=True)
class Neighbour:
    """A classified product as the index holds it: no model instance, so the
    index is built once and compared against without a query."""

    product_id: int
    raw_name: str
    supplier_id: int
    supplier_name: str
    stock_type_id: int
    stock_type_name: str
    stock_type_unit: str
    stock_type_category: str
    stock_equivalent: Decimal
    shape: NameShape


@dataclass(frozen=True)
class NeighbourMatch:
    neighbour: Neighbour
    exact_words: bool
    same_sizes: bool
    same_numbers: bool
    same_supplier: bool
    # The loose tier: ("product" | "neighbour", the word) when one of the two
    # names says one word more than the other; None when they say the same.
    extra_word: tuple[str, str] | None = None
    # Every word paired with its counterpart spelled the same or as its
    # plural (`exact_or_plural`) - no abbreviation, no typo forgiven. Always
    # True when `exact_words`; meaningless (False) for the loose tier.
    words_sure: bool = True
    # The loose tier where the shorter name says ONE word: thin evidence.
    one_word_name: bool = False

    @property
    def same_words(self) -> bool:
        return self.extra_word is None

    @property
    def article_measured(self) -> bool:
        """The neighbour's article is tracked by volume or weight, so a size
        is a conversion factor rather than a different thing."""
        return self.neighbour.stock_type_unit != UnitChoices.UNIT

    @property
    def rank(self) -> tuple:
        """What makes one neighbour a better witness than another, most
        telling first: saying the same words, the same sizes, saying them
        without an abbreviation to guess at, the very same spelling, the
        same supplier (its own catalogue's words), the same pack."""
        return (
            self.same_words,
            self.same_sizes,
            self.words_sure,
            self.exact_words,
            self.same_supplier,
            self.same_numbers,
        )

    @property
    def article_confidence(self) -> str:
        """Measured by strict leave-one-out (scratchpad loo_pipeline.py and
        fix2_measure.py; the counts stay there). "high" needs the same words
        spelled the same (a plural allowed - an abbreviation pairs two
        different words as readily as two spellings of one, and no benchmark
        over names already filed can see the collision coming) AND either an
        article tracked by volume or weight, where the size is a conversion
        factor, or - for an article counted by the unit - the same sizes and
        the same pack: at the same sizes and another pack the only wrong
        articles of that tier were found, so the article and the factor each
        hold their own guard rather than one leaning on the other. The same
        words at another size for an article counted by the unit is the cup
        case - a 25cl cup beside a 50cl one - right one time in three: low.
        A word apart is medium (nine in ten), low when the shorter name has
        one word. Nothing here reads `same_supplier`: it ranks witnesses,
        it does not make one sure - the same supplier AND the same pack
        looked like a sure tier while the judged product was still teaching
        the index its own extra word, and was not once it stopped."""
        if not self.same_words:
            return "low" if self.one_word_name else "medium"
        if not self.words_sure:
            return "medium"
        if self.article_measured:
            return "high"
        if self.same_sizes:
            return "high" if self.same_numbers else "medium"
        return "low"


class ClassifiedNeighbours:
    """The classified products, indexed once for `find`."""

    def __init__(self, neighbours: list[Neighbour]):
        self.neighbours = [neighbour for neighbour in neighbours if neighbour.shape.words]
        # Which articles each exact set of words has been filed under, for
        # `tells_apart`; and which articles each WORD is printed under, for
        # `two_words` - the owner's vocabulary, spelling by spelling.
        self._articles_by_words: dict[frozenset[str], set[int]] = defaultdict(set)
        self._articles_by_word: dict[str, set[int]] = defaultdict(set)
        for neighbour in self.neighbours:
            self._articles_by_words[neighbour.shape.words].add(neighbour.stock_type_id)
            for word in neighbour.shape.words:
                self._articles_by_word[word].add(neighbour.stock_type_id)
        self._tells_apart: dict[str, bool] = {}

    def two_words(self, a: str, b: str) -> bool:
        """Whether the classified names print BOTH spellings, each only under
        articles the other never appears under: two words, not one
        abbreviated. « VIN » under the wines and « VINAIGRE » under the
        vinegars are two; « GOB » and « GOBELET » both under the cups are one
        word printed two ways; a spelling the owner never prints (« GOBELET »
        where every name says « GOB ») refutes nothing. A plural is never two
        words, whatever the articles print: it is the same word by
        construction, what a sure suggestion rests on."""
        if exact_or_plural(a, b):
            return False
        articles_a, articles_b = self._articles_by_word.get(a), self._articles_by_word.get(b)
        return bool(articles_a) and bool(articles_b) and not (articles_a & articles_b)

    def alike(self, a: str, b: str) -> bool:
        """`same_word`, minus the prefix pairings the owner's own vocabulary
        refutes (`two_words`). A typo pairing is not learned: measured on the
        owner's names it paired one word to one word (scratchpad
        fix2_measure.py)."""
        if not same_word(a, b):
            return False
        short, long = sorted((a, b), key=len)
        return not (long.startswith(short) and self.two_words(short, long))

    def tells_apart(self, word: str) -> bool:
        """Whether this one word, on its own, already separates two of the
        owner's articles: some classified product says it, and the same
        words without it are another classified product filed under
        another article. « CONSIGNE » does (a keg and its deposit), so does
        « ZERO » (a cola and its zero-sugar twin) - learned from the
        classifications, never listed here. A loose match on such a word
        is refused: the word is the difference, not noise. Learned in every
        spelling `alike` accepts: a deposit printed « CONS. » teaches
        « CONSIGNE » too, or the next one printed in full was offered the
        goods - and a spelling the vocabulary refutes teaches nothing."""
        if word not in self._tells_apart:
            self._tells_apart[word] = any(
                self._articles_by_words.get(words - {printed}, set()) - articles
                for words, articles in self._articles_by_words.items()
                for printed in words
                if self.alike(printed, word)
            )
        return self._tells_apart[word]

    @classmethod
    def from_database(cls) -> ClassifiedNeighbours:
        from .models import Product

        classified = Product.objects.filter(stock_type__isnull=False, is_expense=False).select_related(
            "stock_type", "supplier"
        )
        return cls([cls.neighbour_of(product) for product in classified])

    @staticmethod
    def neighbour_of(product) -> Neighbour:
        return Neighbour(
            product_id=product.pk,
            raw_name=product.raw_name,
            supplier_id=product.supplier_id,
            supplier_name=product.supplier.name,
            stock_type_id=product.stock_type_id,
            stock_type_name=product.stock_type.name,
            stock_type_unit=product.stock_type.unit,
            stock_type_category=product.stock_type.category,
            stock_equivalent=product.stock_equivalent,
            shape=NameShape.of(product.raw_name),
        )

    def find(self, product) -> NeighbourMatch | None:
        """The one classified product `product` is the same thing as, or
        None: when nothing says the same words, or when the best witnesses
        for two different articles are as good as each other - no guess
        between two candidates, the rule `matching.ocr_match` follows."""
        shape = NameShape.of(product.raw_name)
        if not shape.words:
            return None
        matches = []
        for neighbour in self.neighbours:
            if neighbour.product_id == product.pk:
                continue
            exact = neighbour.shape.words == shape.words
            extra_word = None
            words_sure = exact
            if not exact and not same_words(shape.words, neighbour.shape.words, self.alike):
                apart = one_word_apart(shape.words, neighbour.shape.words, self.alike)
                if apart is None:
                    continue
                extra_word = ("product" if apart[0] == "a" else "neighbour", apart[1])
                if self.tells_apart(apart[1]):
                    continue
                words_sure = False
            elif not exact:
                words_sure = same_words(shape.words, neighbour.shape.words, exact_or_plural)
            same_sizes = neighbour.shape.sizes == shape.sizes
            if extra_word is not None and not same_sizes and neighbour.stock_type_unit == UnitChoices.UNIT:
                # A word apart AND another size, for an article counted by
                # the unit: measured right less than half the time. Not a
                # witness.
                continue
            matches.append(
                NeighbourMatch(
                    neighbour,
                    exact_words=exact,
                    same_sizes=same_sizes,
                    same_numbers=neighbour.shape.numbers == shape.numbers,
                    same_supplier=neighbour.supplier_id == product.supplier_id,
                    extra_word=extra_word,
                    words_sure=words_sure,
                    one_word_name=extra_word is not None and min(len(shape.words), len(neighbour.shape.words)) == 1,
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda match: match.rank, reverse=True)
        best = matches[0]
        for other in matches[1:]:
            if other.neighbour.stock_type_id != best.neighbour.stock_type_id:
                if other.rank == best.rank:
                    return None
                break
        return best


def _quantity_display(value: Decimal) -> str:
    """0.7, 10 - not 0.7000, nor the 1E+1 normalize() makes of 10."""
    return format(Decimal(value).normalize(), "f")


def _line_settles(line, article_measured: bool) -> str | None:
    """What the product's OWN invoice line settles about its conversion
    factor, whatever any name prints - the reason the factor is 1, or None.

    For an article tracked by volume or weight, a line that PRINTS its
    volume or weight already counts in litres or kilos: `product_base_amount`
    reads `total_volume`, so any other factor multiplies litres by a bottle
    size (on the owner's data, not one measured product carries another
    factor; the printed size is the convention of the UNMEASURED lines). For
    an article counted by the unit, a colisage above one means the line's
    quantity already counts the items (every such product of the owner's is
    at 1). Both hold whichever supplier printed the line, and both are
    arithmetic the application does, not a reading."""
    if line is None:
        return None
    if article_measured and line.total_volume and line.total_volume > 0:
        return "cette ligne imprime son volume ou son poids (la quantité est déjà en litres ou en kilos)"
    if not article_measured and line.colisage != 1:
        return f"cette ligne compte déjà les unités (colis de {line.colisage})"
    return None


def _reads_a_figure(guess, match: NeighbourMatch) -> bool:
    """Whether the quantity extractor, reading this product's own name and
    line for the neighbour's kind of article, read a FIGURE at all: a size
    or a count it is at least medium-sure of, in the article's own unit. Its
    default (« aucun indice », 1 at low) and a reading for the other kind of
    article (a count where the article is in litres) are not figures - they
    say nothing about the factor."""
    measured = guess.suggested_stock_unit != UnitChoices.UNIT
    fits = measured == match.article_measured and (
        not measured or guess.suggested_stock_unit == match.neighbour.stock_type_unit
    )
    return fits and guess.confidence != "low"


def _reads_otherwise(guess, match: NeighbourMatch, copied: Decimal) -> bool:
    """The extractor read a figure (`_reads_a_figure`) that differs from the
    copied factor."""
    return _reads_a_figure(guess, match) and guess.stock_equivalent != copied


def _reads_the_same(guess, match: NeighbourMatch, copied: Decimal) -> bool:
    """The extractor read a figure (`_reads_a_figure`) that IS the copied
    factor: the one thing that can vouch for a copy no printed number
    supports."""
    return _reads_a_figure(guess, match) and guess.stock_equivalent == copied


def _neighbour_suggestion(product, match: NeighbourMatch) -> dict:
    """The neighbour's article, and a conversion factor for THIS product.

    The factor is copied from the neighbour when the two names are the same
    pack (the same numbers printed), and "high" only when nothing on the
    product's own line says otherwise - measured strictly (scratchpad
    fix2_measure.py), a copy the line's own reading agrees with was right
    every time. Two things the line can say, in this order:
    - `_line_settles`: a printed volume, or a colisage already counting the
      items, make the factor 1 by the application's own arithmetic. Against
      a copied factor that is not 1, the two disagree, and both are said.
    - `_reads_otherwise`: the extractor read a figure for the article's
      kind that is not the copied one (the same numbers, another
      convention). The owner's convention for the same numbers is kept and
      the line's reading said beside it, at medium: a person decides.
    When neither name prints a number, two EMPTY signatures are equal and
    nothing printed says they are one pack: the copy is sure only when the
    line's own reading is that very figure (`_reads_the_same`) and medium
    otherwise, worded « aucun nombre imprimé : conversion du voisin reprise,
    à vérifier » - whoever sold it; the same supplier looked like a witness
    for it (measured, its copies were right) but prints nothing more than
    another. `factor_rule` names which branch answered, for the benchmark."""
    neighbour = match.neighbour
    article_measured = match.article_measured
    line = product.invoice_lines.first()
    guess = extract_quantity_for_product(product, assume_volume_tracked=article_measured, line=line)
    settled = _line_settles(line, article_measured)
    if match.same_numbers:
        copied = neighbour.stock_equivalent
        shown = _quantity_display(copied)
        if settled and copied != 1:
            stock_equivalent, factor_confidence, factor_rule = Decimal("1"), "medium", "line-contradicts-copy"
            factor_note = f"le voisin est à {shown}, mais {settled} : 1, à vérifier"
        elif settled:
            stock_equivalent, factor_confidence, factor_rule = copied, "high", "copy-confirmed-by-line"
            factor_note = f"même conditionnement, conversion reprise (1 produit = {shown}), et {settled}"
        elif _reads_otherwise(guess, match, copied):
            stock_equivalent, factor_confidence, factor_rule = copied, "medium", "copy-read-otherwise"
            factor_note = (
                f"conversion du voisin reprise (1 produit = {shown}), mais lue autrement sur cette ligne "
                f"({_quantity_display(guess.stock_equivalent)} : {guess.note}) : à vérifier"
            )
        elif not neighbour.shape.numbers:
            if _reads_the_same(guess, match, copied):
                stock_equivalent, factor_confidence, factor_rule = copied, "high", "copy-no-number-read-agrees"
                factor_note = (
                    f"aucun nombre imprimé, mais la ligne se lit de même ({guess.note}) : "
                    f"conversion reprise (1 produit = {shown})"
                )
            else:
                stock_equivalent, factor_confidence, factor_rule = copied, "medium", "copy-no-number"
                factor_note = f"aucun nombre imprimé : conversion du voisin reprise, à vérifier (1 produit = {shown})"
        else:
            stock_equivalent, factor_confidence, factor_rule = copied, "high", "copy"
            factor_note = f"même conditionnement, conversion reprise (1 produit = {shown})"
    elif settled:
        stock_equivalent, factor_confidence, factor_rule = Decimal("1"), "high", "line-settles"
        factor_note = f"conditionnement différent, mais {settled} : 1"
    else:
        # The neighbour's factor is its own pack's. This product's is read
        # off its own name, knowing the article: a 33cl bottle of an article
        # in litres is 0,33 whatever the small-format shortcut would say.
        measured = guess.suggested_stock_unit != UnitChoices.UNIT
        if measured != article_measured or (measured and guess.suggested_stock_unit != neighbour.stock_type_unit):
            stock_equivalent, factor_confidence, factor_rule = Decimal("1"), "low", "other-pack-unreadable"
            factor_note = f"conditionnement différent, conversion à vérifier ({guess.note})"
        else:
            stock_equivalent, factor_rule = guess.stock_equivalent, "other-pack-read"
            # For an article tracked by volume or weight the factor is the
            # size printed on the bottle, whoever sells it. For one counted
            # by the unit it is a pack count, and how many a line counts is
            # the supplier's convention (a case of 24 as one line, or as
            # 24): another pack than the neighbour's is a factor to look at,
            # never one to approve in bulk - measured, the name's own count
            # was wrong about one time in ten there (loo_pipeline.py).
            factor_confidence = guess.confidence if article_measured else least_confident(guess.confidence, "medium")
            factor_note = f"conditionnement différent, conversion estimée : {guess.note}"
    if match.same_words:
        likeness = f"Même chose que « {neighbour.raw_name} » ({neighbour.supplier_name})"
        if not match.words_sure:
            likeness += ", à une abréviation ou une faute près"
        if not match.same_sizes and not article_measured:
            likeness += ", à une autre taille (vous rangez les tailles à part : sans doute un autre article)"
    else:
        # `same_words` is `extra_word is None`, a narrowing ty cannot see
        # through the property.
        side, word = cast("tuple[str, str]", match.extra_word)
        whose = "ce produit" if side == "product" else "le voisin"
        likeness = (
            f"Proche de « {neighbour.raw_name} » ({neighbour.supplier_name}), "
            f"à un mot près - {whose} dit en plus « {word} »"
        )
        if match.one_word_name:
            likeness += " (un seul mot en commun)"
    return {
        "source": "neighbour",
        "stock_type_name": neighbour.stock_type_name,
        "new_stock_type_category": neighbour.stock_type_category,
        "new_stock_type_unit": neighbour.stock_type_unit,
        "stock_equivalent": stock_equivalent,
        "confidence": least_confident(match.article_confidence, factor_confidence),
        "reasoning": f"{likeness}, déjà rangé dans « {neighbour.stock_type_name} » ; {factor_note}.",
        "matched_stock_type_id": neighbour.stock_type_id,
        "is_new_stock_type": False,
        "neighbour_product_id": neighbour.product_id,
        "factor_rule": factor_rule,
    }


def _rule_suggestion(product, rule: MatchRule) -> dict:
    guess = extract_quantity_for_product(
        product,
        force_unit_count=rule.force_unit_count,
        assume_volume_tracked=rule.assume_volume_tracked,
    )
    # Said in words, never as the regular expression: the panel is the
    # owner's screen, and « \bVODKA\b|\bVDK\b » is nobody's explanation.
    hit = rule.pattern.search(product.raw_name.upper())
    matched = hit.group(0).strip() if hit and hit.group(0).strip() else rule.stock_type_name.upper()
    article = _normalize_casing(rule.stock_type_name)
    return {
        "source": "rule",
        "stock_type_name": article,
        "new_stock_type_category": _normalize_casing(rule.category),
        "new_stock_type_unit": rule.unit,
        "stock_equivalent": guess.stock_equivalent,
        "confidence": least_confident(RULE_ARTICLE_CONFIDENCE, guess.confidence),
        "reasoning": f"Règle : « {matched} » dans le nom → {article} ; {guess.note}",
    }


def _fallback_suggestion(product, category_words: dict[str, Counter]) -> dict:
    clean_name = _LEADING_JUNK_RE.sub("", product.raw_name)
    assume_volume_tracked = bool(_BITTER_LIKE_RE.search(clean_name.upper()))
    guess = extract_quantity_for_product(product, assume_volume_tracked=assume_volume_tracked)
    category = guess_category(clean_name, category_words) or _UNKNOWN_CATEGORY
    return {
        "source": "fallback",
        "stock_type_name": _normalize_casing(strip_size_and_count_tokens(clean_name)),
        "new_stock_type_category": _normalize_casing(category),
        "new_stock_type_unit": guess.suggested_stock_unit,
        "stock_equivalent": guess.stock_equivalent,
        "confidence": guess.confidence,
        "reasoning": f"Aucun produit classé ni règle ne le reconnaît, nom de facture repris tel quel. {guess.note}",
    }


def classified_fingerprint() -> str:
    """What every suggestion is a claim about: which product is filed under
    which article at which factor, and what the articles are called. A
    suggestion carries the fingerprint it was made against
    (`classified_fingerprint` in the dict) and is made again once it
    differs - a neighbour moved to another article, an undo, a merge, a
    rename - because a stored suggestion is a snapshot: kept, « Approuver
    les sûres » booked the article the neighbour no longer belonged to, and
    the twin the owner had just filed taught nothing to the packs already
    suggested. Two cheap queries, a few thousand rows at most."""
    from .models import Product

    digest = hashlib.sha1()
    classified = (
        Product.objects.filter(stock_type__isnull=False, is_expense=False)
        .order_by("pk")
        .values_list("pk", "stock_type_id", "stock_equivalent")
    )
    for row in classified:
        digest.update(repr(row).encode())
    for row in StockType.objects.order_by("pk").values_list("pk", "name", "unit", "category"):
        digest.update(repr(row).encode())
    return digest.hexdigest()


def is_current(suggestion: dict | None, fingerprint: str) -> bool:
    """Whether a stored suggestion was made against these classifications.
    One made before they moved - or before suggestions carried the
    fingerprint at all - is not."""
    return bool(suggestion) and suggestion.get("classified_fingerprint") == fingerprint


class SuggestionContext:
    """What every suggestion of one pass shares, built once and only when
    first needed: the classified products, the category vocabulary and the
    fingerprint of the classifications the pass is made against."""

    def __init__(self, neighbours: ClassifiedNeighbours | None = None, category_words=None, fingerprint=None):
        self._neighbours = neighbours
        self._category_words = category_words
        self._fingerprint = fingerprint

    @property
    def neighbours(self) -> ClassifiedNeighbours:
        if self._neighbours is None:
            self._neighbours = ClassifiedNeighbours.from_database()
        return self._neighbours

    @property
    def category_words(self) -> dict[str, Counter]:
        if self._category_words is None:
            self._category_words = build_category_classifier()
        return self._category_words

    @property
    def fingerprint(self) -> str:
        if self._fingerprint is None:
            self._fingerprint = classified_fingerprint()
        return self._fingerprint


def suggest_for_product(product, context: SuggestionContext | None = None) -> dict:
    """The suggestion for one product, in the order the panel states:
    a classified neighbour, then the rules, then the raw name. Always
    something - a product in the queue is never blank - resolved against the
    articles that exist (`_resolve_stock_type_match`), its confidence that
    of its weakest part (`least_confident`), and stamped with the
    classifications it was made against (`classified_fingerprint`)."""
    context = context or SuggestionContext()
    match = context.neighbours.find(product)
    if match is not None:
        suggestion = _neighbour_suggestion(product, match)
    else:
        rule = match_stock_type(product.raw_name)
        if rule is not None:
            suggestion = _rule_suggestion(product, rule)
        else:
            suggestion = _fallback_suggestion(product, context.category_words)
    _resolve_stock_type_match(suggestion)
    if suggestion["is_new_stock_type"]:
        suggestion["confidence"] = least_confident(suggestion["confidence"], NEW_ARTICLE_CONFIDENCE)
    elif suggestion["source"] == "fallback":
        suggestion["confidence"] = least_confident(suggestion["confidence"], FALLBACK_EXISTING_ARTICLE_CONFIDENCE)
    suggestion["classified_fingerprint"] = context.fingerprint
    return suggestion


def apply_rules_to_pending_products() -> Counter:
    """Gives every pending product a suggestion (`suggest_for_product`),
    stored in `ai_suggestion` - the review panel doesn't need to know which
    source filled it in - unless it already holds one made against the
    classifications as they stand (`is_current`). Returns how many each
    source answered for in this pass.

    Synchronous and instant: the neighbours are indexed once per call and
    compared in memory, the rules are a regex scan, so unlike the Ollama
    version this needs no background job, no polling, no cancel button. The
    pass after a classification remakes every pending suggestion - a few
    milliseconds each - which is how a twin the owner just filed teaches
    the packs still waiting.
    """
    from .models import Product

    pending = Product.objects.filter(stock_type__isnull=True, is_expense=False)
    context = SuggestionContext()
    sources: Counter = Counter()
    for product in pending:
        if is_current(product.ai_suggestion, context.fingerprint):
            continue
        suggestion = suggest_for_product(product, context)
        product.ai_suggestion = suggestion
        product.save(update_fields=["ai_suggestion"])
        sources[suggestion["source"]] += 1
    return sources
