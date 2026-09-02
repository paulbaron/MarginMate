"""Hardcoded product-name -> stock item rules, replacing an earlier
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

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .models import StockType, UnitChoices
from .quantity_extraction import strip_size_and_count_tokens


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
    "DE", "DU", "DES", "LA", "LE", "LES", "ET", "EN", "AU", "AUX", "A",
    "AVEC", "SANS", "POUR", "SUR", "UN", "UNE", "SA",
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
    lookup itself."""
    suggestion["stock_type_name"] = _normalize_casing(suggestion.get("stock_type_name") or "")
    suggestion["new_stock_type_category"] = _normalize_casing(suggestion.get("new_stock_type_category") or "")
    name = suggestion["stock_type_name"]
    match = StockType.objects.filter(name__iexact=name).first() if name else None
    suggestion["is_new_stock_type"] = match is None
    suggestion["matched_stock_type_id"] = match.id if match else None
    # Pre-format so the review form's editable input doesn't show something
    # like "0.7000000000000001", and so a Decimal never ends up in a dict
    # that's about to be saved into a JSONField (json.dumps doesn't know how
    # to serialize one).
    try:
        suggestion["stock_equivalent"] = f"{float(suggestion.get('stock_equivalent', 1)):g}"
    except (TypeError, ValueError):
        suggestion["stock_equivalent"] = "1"


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


# --- Consigne (crate/keg/jug deposits - PLEIN=charge, VIDE=refund; see
# invoices/parsers/metro.py for how these end up with a negative
# quantity/total on the refund line). Always exactly 1 - a crate is a
# single returnable object regardless of what it holds or what number the
# name happens to mention (a crate marked "24X33CL" describes the crate's
# CONTENTS; the deposit is charged per crate, not per bottle inside it).
_CONSIGNE_RULES = [
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

# --- Food / épicerie -------------------------------------------------------
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
    *_CONSIGNE_RULES,
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


def apply_rules_to_pending_products() -> tuple[int, int]:
    """Runs every pending product without a suggestion yet through the rules
    above, storing a match in the same `ai_suggestion` shape the (now
    unused) Ollama path used - the review queue template doesn't need to
    know or care which path filled it in. Returns (rule_matched, fallback).

    A product no rule recognises is never left blank: it falls back to its
    own raw invoice name as the stock item (source="fallback" in the
    suggestion, so the review queue can flag it as unverified), so every
    product in the queue is autofilled and ready to approve or rename -
    coverage over precision, on the understanding that manual review/merge
    (see StockTypeUpdateView) happens afterward, not that every guess here
    is correct.

    Synchronous and instant (a regex scan over ~200 rules per product), so
    unlike the Ollama version this needs no background job, no polling, no
    cancel button - it's done before the request that triggered it would
    have even gotten Ollama's first token back.
    """
    from .models import Product
    from .quantity_extraction import extract_quantity_for_product

    pending = Product.objects.filter(stock_type__isnull=True, ai_suggestion__isnull=True)
    rule_matched = 0
    fallback = 0
    category_words: dict[str, Counter] | None = None
    for product in pending:
        rule = match_stock_type(product.raw_name)
        if rule is not None:
            guess = extract_quantity_for_product(
                product,
                force_unit_count=rule.force_unit_count,
                assume_volume_tracked=rule.assume_volume_tracked,
            )
            suggestion = {
                "source": "rule",
                "stock_type_name": _normalize_casing(rule.stock_type_name),
                "new_stock_type_category": _normalize_casing(rule.category),
                "new_stock_type_unit": rule.unit,
                "stock_equivalent": guess.stock_equivalent,
                "confidence": guess.confidence,
                "reasoning": guess.note,
            }
            rule_matched += 1
        else:
            if category_words is None:
                category_words = build_category_classifier()
            clean_name = _LEADING_JUNK_RE.sub("", product.raw_name)
            assume_volume_tracked = bool(_BITTER_LIKE_RE.search(clean_name.upper()))
            guess = extract_quantity_for_product(product, assume_volume_tracked=assume_volume_tracked)
            category = guess_category(clean_name, category_words) or _UNKNOWN_CATEGORY
            suggestion = {
                "source": "fallback",
                "stock_type_name": _normalize_casing(strip_size_and_count_tokens(clean_name)),
                "new_stock_type_category": _normalize_casing(category),
                "new_stock_type_unit": guess.suggested_stock_unit,
                "stock_equivalent": guess.stock_equivalent,
                "confidence": guess.confidence,
                "reasoning": f"Aucune règle reconnue, nom de facture repris tel quel. {guess.note}",
            }
            fallback += 1
        _resolve_stock_type_match(suggestion)
        product.ai_suggestion = suggestion
        product.save(update_fields=["ai_suggestion"])

    return rule_matched, fallback
