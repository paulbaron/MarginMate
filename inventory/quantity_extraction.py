"""Deterministic quantity/unit extraction from a raw supplier product name,
cross-checked against the invoice line's own colisage/quantity/total_volume.

This replaces what used to be an LLM's job: parsing a pack size or count out
of the product name is mechanical pattern-matching, not language
understanding, and a small local model was measurably unreliable at it
(missed pack counts, double-counted a colisage that was already correct,
misread physical dimensions as quantities). A regex-based extractor is
instant, free, and - validated against every real Metro product in this
database - right about 85% of the time with full confidence, and honestly
uncertain (falling back to 1, never a wrong guess) the rest of the time. See
inventory/product_matching_rules.py for the (also now regex-based) job of
naming which stock item a product belongs to - a separate concern from the
quantity extraction here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

VOLUME_UNITS = {"ML": Decimal("0.001"), "CL": Decimal("0.01"), "L": Decimal("1")}
WEIGHT_UNITS = {
    "MG": Decimal("0.000001"),
    "GRS": Decimal("0.001"),
    "GR": Decimal("0.001"),
    "G": Decimal("0.001"),
    "KG": Decimal("1"),
}
ALL_UNIT_TOKENS = "ML|CL|KG|MG|GRS|GR|G|L"  # GRS/GR are common French abbreviations
# for grammes alongside plain G - a trailing \b after whichever one matches is what
# stops any of them from firing inside an unrelated word ("GRAIN" never matches
# "GR" here, since nothing in the alternation is followed by a word boundary until
# the "N" ends the word).
LENGTH_UNIT_TOKENS = "MM|CM|M"  # physical size only - never a count, never a stock size

# Weight/volume "does this name mention one at all" sniffs, used only to
# pick L vs KG once a quantity is already known (see is_weight below) - NOT
# for extracting the quantity itself (SIZE_UNIT_RE and friends do that, by
# explicitly capturing the leading digit run instead of leaning on \b).
# `\b` alone doesn't work as the LEADING edge here: \b needs a transition
# between a word char and a non-word char, and a digit is itself a word
# char, so plain \b(KG)\b never matches a fused "2,5KG" or "800G" (no
# boundary between the "5"/"0" and the "K"/"G"). Blocking only a preceding
# LETTER - not a preceding digit - fixes that while still keeping this from
# firing inside an unrelated word.
WEIGHT_TOKEN_RE = re.compile(r"(?<![A-Z])(KG|GRS?|MG|G)\b")
VOLUME_TOKEN_RE = re.compile(r"(?<![A-Z])(CL|ML|L)\b")

HOUSE_BRAND_PREFIX = re.compile(r"^(?:MPRO|ARO|MC|ROCH|RIOBA)\b\s*")

# Pure W x H (x D) dimension - digits joined by X with no volume/weight unit
# letters attached, not bordering another digit/letter/decimal-separator (so
# it doesn't clip a bigger number or get confused with SIZE_X_COUNT below).
# An optional trailing length unit is swallowed too ("70X45MM") so it can't
# be left dangling for TRAILING_FUSED_COUNT_RE to misread as "count=45".
DIMENSION_RE = re.compile(
    rf"(?<![A-Z0-9.,])\d+(?:[.,]\d+)?X\d+(?:[.,]\d+)?(?:X\d+(?:[.,]\d+)?)?(?:{LENGTH_UNIT_TOKENS})?(?![A-Z0-9.,])"
)

# a standalone physical length with no "X" involved, e.g. "20CM" (a straw's
# length) or "D28CM" (a lid's diameter, fused onto the "D" abbreviation) -
# discarded the same way, it's a size descriptor of the product, not a
# quantity of anything. Same reasoning as SIZE_UNIT_RE below for only
# blocking a preceding digit, not a preceding letter.
LENGTH_RE = re.compile(rf"(?<!\d)(\d+(?:[.,]\d+)?)\s*({LENGTH_UNIT_TOKENS})\b")

# <size><unit>X<count>, e.g. "10GX100", "60GX10"
SIZE_X_COUNT_RE = re.compile(rf"(\d+(?:[.,]\d+)?)({ALL_UNIT_TOKENS})X(\d+)\b")

# <count>X<size><unit>, e.g. "24X33CL", "6X35.5CL"
COUNT_X_SIZE_RE = re.compile(rf"(?<![A-Z0-9.,])(\d+)X(\d+(?:[.,]\d+)?)\s*({ALL_UNIT_TOKENS})\b")

# <size><unit> X <count> with real spaces around the "X", e.g. "33 CL X 24" -
# only used to sanity-check Case 0's total_volume/qty division below, not by
# the main name-parsing pipeline (which handles the fused "24X33CL" shape via
# the two patterns above; a spaced-out count is ambiguous enough elsewhere
# that it's only trusted here, where a measured total_volume already backs
# it up).
SPACED_SIZE_X_COUNT_RE = re.compile(rf"(\d+(?:[.,]\d+)?)\s*({ALL_UNIT_TOKENS})\s*X\s*(\d+)\b")

# standalone size+unit (fused or spaced), applied after the two patterns above
# have already claimed their matches. The lookbehind only blocks a preceding
# DIGIT (so this can't clip the tail of a bigger number) - a preceding LETTER
# is allowed on purpose, since a size is very often fused straight onto the
# preceding word with no space at all ("...PET2L" = a 2L PET bottle).
SIZE_UNIT_RE = re.compile(rf"(?<!\d)(\d+(?:[.,]\d+)?)\s*({ALL_UNIT_TOKENS})\b")

# trailing " X<N>" - the leading space is what distinguishes a real standalone
# count from the second half of a fused dimension like "20X20"
TRAILING_COUNT_RE = re.compile(r"(?<=\s)X(\d+)\s*$")

# same standalone "X<N>" multiplier, but anywhere in the name, not just at
# the very end (e.g. "ORGEAT 1L X 1 VP" - the "X 1" sits before a trailing
# "VP" that TRAILING_COUNT_RE's end anchor would otherwise miss). Only used
# for name-cleaning (strip_size_and_count_tokens) - extract_quantity itself
# deliberately keeps its own count-vs-size disambiguation anchored to the
# true string end, per TRAILING_COUNT_RE's own comment above.
MID_COUNT_RE = re.compile(r"(?<=\s)X\s*\d+(?=\s|$)")

# trailing count fused directly onto a word with no "X" at all, e.g. "24RLX"
# (24 rolls). Run BEFORE SIZE_UNIT_RE (on the dimension/length-stripped
# string only) deliberately: masking a real trailing size like "...1.5L" for
# 7UP would otherwise leave "7UP" looking like it's now at the end of the
# string once that mask is in place, and "7UP" fits this same shape (digit
# then 2+ letters at the end) - the fix is to look at the true end of the
# name before any such masking has had a chance to move it. KNOWN_UNIT_WORDS
# is a second, independent guard against the same failure mode (e.g. if a
# unit ever isn't stripped first for some other reason). The 2-letter
# minimum on its own already keeps single-letter tokens (G, L) out.
KNOWN_UNIT_WORDS = {"ML", "CL", "KG", "MG", "GRS", "GR", "MM", "CM"}
TRAILING_FUSED_COUNT_RE = re.compile(r"(\d+)([A-Z]{2,})\s*$")

# leading count before the first real word - space-separated ("100 SERV"), or
# fused directly onto it ("100SAC") but ONLY once a house-brand prefix has
# actually been stripped first (see `had_prefix` below) - otherwise a fused
# leading number is just as likely to be a brand name that happens to start
# with a digit ("7UP") as a real count, and there's no way to tell those
# apart from shape alone.
LEADING_COUNT_RE = re.compile(r"^(\d+)\s*(?=[A-Z])")
LEADING_COUNT_SPACED_RE = re.compile(r"^(\d+)\s+(?=[A-Z])")

ENV_RE = re.compile(r"\bENV\b")

# A CL/L/ML size next to one of these describes the CONTAINER's own
# capacity (a drinking glass, a soap dispenser's reservoir), not an amount
# of liquid product to track - "VERRES BIERE LILITH 58CL" is 58cl-capacity
# glasses, not 58cl of something poured. A small, closed linguistic class of
# vessel/dispenser words, not a per-product list - the same reasoning as
# CONSIGNE_RULES treating a crate's contents as separate from what the
# crate itself is worth. "LAVE VERRE"/"LAVE-VERRE" is excluded on purpose -
# that's glass-WASHING liquid (a real volume-tracked cleaning product, like
# "LAVE VITRE"), not glassware itself.
CONTAINER_WORD_RE = re.compile(r"(?<!LAVE[ -])\b(VERRES?|VAP|GOB(?:ELETS?)?|TASSES?|MUGS?|BOLS?|DISTRIB\w*)\b")

# Standard European food-service can/tin format codes - "4/4" (equivalently
# "1/1") is the full-size catering tin, "1/2" and "1/4" fractions of it, "3/1"
# and "5/1" multiples for bulk catering formats. The values below are the
# commonly-cited approximate net weights for each - genuinely approximate
# (the real weight varies by product: tomatoes vs. beans vs. corn), so this
# is always marked `approx` unlike a directly-printed "800G".
CAN_FORMAT_KG = {
    "1/8": Decimal("0.125"),
    "1/4": Decimal("0.2"),
    "1/2": Decimal("0.4"),
    "1/1": Decimal("0.85"),
    "4/4": Decimal("0.85"),
    "3/1": Decimal("2.5"),
    "5/1": Decimal("4.25"),
    "10/1": Decimal("10"),
}
CAN_FORMAT_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in CAN_FORMAT_KG) + r")\b")

# Size alone decides unit-vs-volume tracking - a can/bottle at 33cl or under
# is counted bottle-by-bottle, over 33cl converts to the stock type's own
# volume/weight instead (a 70cl spirit, a 75cl Prosecco). Deliberately NOT
# based on packaging material (PET/VP/VC in the name) - glass vs. plastic
# doesn't change how much liquid is in the bottle.
SMALL_FORMAT_VOLUME_THRESHOLD = Decimal("0.33")  # litres - at/below this is UNIT, above is volume-tracked


def _to_decimal(s: str) -> Decimal:
    try:
        return Decimal(s.replace(",", "."))
    except InvalidOperation:
        return Decimal("0")


def _mask(name: str, match: re.Match) -> str:
    start, end = match.span()
    return name[:start] + " " * (end - start) + name[end:]


@dataclass
class QuantityGuess:
    # "UNIT" | "L" | "KG" - which invoice-line field this stock_equivalent
    # assumes as the base amount (quantity vs total_volume; see
    # product_base_amount in services.py). Internal to how stock_equivalent
    # was derived, not persisted anywhere: the actual product.unit is always
    # set to match its stock type's own unit at assignment time (see
    # assign_product in views.py) rather than read from here, since Litre
    # vs Kilogramme never changes product_base_amount's behaviour anyway -
    # only "UNIT" vs not does, and force_unit_count/assume_volume_tracked
    # already keep stock_equivalent itself correct for either case.
    product_unit: str
    stock_equivalent: Decimal
    confidence: str  # "high" | "medium" | "low"
    note: str
    approx: bool = False
    debug: dict = field(default_factory=dict)
    # What unit the STOCK TYPE itself should track in, as opposed to
    # product_unit (how to read THIS product's own invoiced quantity) - e.g.
    # for "POITRINE FUMEE TRANCHE 800G" product_unit stays "UNIT" (1 pack)
    # with stock_equivalent=0.8, but the stock item accumulating those packs
    # should be tracked in "KG" overall. Only meaningful as a fallback for a
    # product no named rule recognises - a matched rule always specifies its
    # own stock type unit deliberately (e.g. Bitter stays LITRE despite being
    # a small, sub-33cl bottle - poured a dash at a time rather than sold
    # whole, a business convention no size heuristic would guess).
    suggested_stock_unit: str = "UNIT"


def extract_quantity(
    raw_name: str,
    colisage: int,
    qty: int,
    total_volume: Decimal,
    force_unit_count: bool = False,
    assume_volume_tracked: bool = False,
) -> QuantityGuess:
    """`force_unit_count` and `assume_volume_tracked` let a matching rule
    override what the name/colisage would otherwise imply, for a whole
    category of product where that general logic is known not to apply:
    `force_unit_count` fixes the product-to-stock ratio at exactly 1:1 - one
    physical item (a crate, a keg) always equals exactly one stock unit,
    regardless of what number happens to be printed in its name (a "24X33CL"
    on a Coca crate describes the crate's contents, not how many stock units
    it's worth); `assume_volume_tracked` for something poured in small doses
    rather than served whole (a bitters bottle, a vermouth) that the
    small-format shortcut below would otherwise wrongly flatten to "1 unit"
    just because the bottle itself is under 33cl.
    """
    name = raw_name.upper()
    # "environ" (approximately) is often fused straight onto the weight it
    # qualifies with no space on either side ("2,5KGENV", "800GENV",
    # "1,2KGENVCPV") - a bare word-boundary regex never matches "KG"/"G" in
    # there (no boundary between a digit/letter and the "E"/"V" it touches),
    # which silently broke both the is_weight check below and ENV_RE's own
    # approx detection. Splitting it back out first fixes both at once, and
    # every other size regex that runs on `name`.
    name = re.sub(r"(?<=[A-Z0-9])ENV", " ENV ", name)
    is_container = bool(CONTAINER_WORD_RE.search(name))

    if force_unit_count:
        return QuantityGuess("UNIT", Decimal("1"), "high", "toujours compté à l'unité (consigne)")

    # Case 0: the supplier already measured this line's own volume/weight -
    # trust it, no name-parsing needed. Which of L/KG it is still has to be
    # guessed from the name, since the field itself doesn't say.
    if total_volume and total_volume != 0:
        is_weight = bool(WEIGHT_TOKEN_RE.search(name)) and not VOLUME_TOKEN_RE.search(name)
        if not is_weight and qty and not assume_volume_tracked:
            # Small-format drinks still get counted bottle-by-bottle even
            # when the invoice happened to also print a correct total volume
            # for the line (e.g. "24 x 33CL = 7.92L") - the per-item size
            # backed out of that total is what the 33cl check needs, since
            # the name alone won't always repeat the size.
            #
            # qty itself can under-count when it only reflects how many
            # CASES were bought, not how many individual items are inside
            # (colisage=1, qty=1, total_volume=7.92 for a case of "33 CL X
            # 24") - trust a per-case count spelled out in the name over qty
            # for this division, same "colisage already accounts for it"
            # rule as everywhere else: only when colisage doesn't already.
            effective_qty = qty
            count_m = COUNT_X_SIZE_RE.search(name) or SPACED_SIZE_X_COUNT_RE.search(name)
            if count_m:
                name_count = int(count_m.group(1) if count_m.re is COUNT_X_SIZE_RE else count_m.group(3))
                if colisage == 1 and name_count != qty:
                    effective_qty = name_count
            per_item = Decimal(total_volume) / Decimal(effective_qty)
            if per_item <= SMALL_FORMAT_VOLUME_THRESHOLD or is_container:
                reason = "contenant (verre/distributeur)" if is_container else f"{per_item}L/article ≤ 33cl"
                return QuantityGuess(
                    "UNIT", Decimal("1"), "high", f"vendu à l'unité ({reason})",
                    debug={"small_format": reason},
                )
        unit = "KG" if is_weight else "L"
        return QuantityGuess(unit, Decimal("1"), "high", "volume/poids déjà mesuré", suggested_stock_unit=unit)

    working = name
    approx = bool(ENV_RE.search(working))

    size_value: Decimal | None = None
    size_unit: str | None = None
    count_value: int | None = None
    debug = {}

    m = SIZE_X_COUNT_RE.search(working)
    if m:
        size_value, size_unit, count_value = _to_decimal(m.group(1)), m.group(2), int(m.group(3))
        working = _mask(working, m)
        debug["size_x_count"] = m.group(0)

    if size_value is None:
        m = COUNT_X_SIZE_RE.search(working)
        if m:
            count_value, size_value, size_unit = int(m.group(1)), _to_decimal(m.group(2)), m.group(3)
            working = _mask(working, m)
            debug["count_x_size"] = m.group(0)

    # Strip pure dimensions (WxH / WxHxD) and standalone lengths ("20CM") now,
    # before generic count/size scans would otherwise misread either half of
    # a dimension, or a length, as a count.
    for m in list(DIMENSION_RE.finditer(working)):
        debug.setdefault("dimensions_ignored", []).append(m.group(0))
        working = _mask(working, m)
    for m in list(LENGTH_RE.finditer(working)):
        debug.setdefault("lengths_ignored", []).append(m.group(0))
        working = _mask(working, m)

    # Trailing-count checks run BEFORE the generic size scan, against the true
    # end of the (dimension/length-stripped) name - not a version that's
    # already had a real trailing size masked out of it. Doing this the other
    # way round was the actual bug behind "7UP 1.5L": once "1.5L" was masked
    # away, "7UP" was left sitting at what now looked like the string's end,
    # and matches the exact same shape (digit + 2 letters, end of string) as
    # a genuine case like "24RLX".
    if count_value is None:
        m = TRAILING_COUNT_RE.search(working)
        if m:
            count_value = int(m.group(1))
            working = _mask(working, m)
            debug["trailing_count"] = m.group(0)

    if count_value is None:
        m = TRAILING_FUSED_COUNT_RE.search(working)
        if m and m.group(2) not in KNOWN_UNIT_WORDS:
            count_value = int(m.group(1))
            working = _mask(working, m)
            debug["trailing_fused_count"] = m.group(0)

    if size_value is None:
        m = CAN_FORMAT_RE.search(working)
        if m:
            size_value, size_unit = CAN_FORMAT_KG[m.group(1)], "KG"
            working = _mask(working, m)
            debug["can_format"] = m.group(0)
            approx = True

    if size_value is None:
        m = SIZE_UNIT_RE.search(working)
        if m:
            size_value, size_unit = _to_decimal(m.group(1)), m.group(2)
            working = _mask(working, m)
            debug["size_unit"] = m.group(0)

    if count_value is None:
        prefix_stripped = HOUSE_BRAND_PREFIX.sub("", working.strip())
        had_prefix = prefix_stripped != working.strip()
        pattern = LEADING_COUNT_RE if had_prefix else LEADING_COUNT_SPACED_RE
        m = pattern.match(prefix_stripped)
        if m:
            count_value = int(m.group(1))
            debug["leading_count"] = m.group(0)

    # A number-as-brand-name at the very start of a line ("1664 BLD 0,0D
    # 33CL VP" - Kronenbourg 1664) is syntactically indistinguishable from a
    # real leading count. No genuine pack count in this dataset goes past a
    # few hundred, so treat anything implausibly large as a brand name
    # instead and fall back to whatever other signal (e.g. the "33CL" size)
    # is left, rather than emitting a nonsense stock_equivalent.
    if count_value is not None and count_value >= 1000:
        debug["count_discarded_implausible"] = count_value
        count_value = None

    # Cross-check any count candidate against what the supplier's own
    # colisage field already captured - the single most important step.
    # Getting this wrong either double-counts (name count layered on top of
    # an already-correct colisage) or under-counts (colisage=1 but the real
    # pack size is only in the name).
    #
    # colisage != 1 is trusted outright, not just when it happens to equal
    # the name's own count exactly: a name can mention a sub-pack size that
    # doesn't match colisage 1-for-1 (a case of 24 sold as "4X33CL" - 4-packs
    # within it) while colisage/quantity have still already correctly
    # counted every bottle. colisage is the supplier's own authoritative
    # count; a number embedded in the name is only ever a fallback for when
    # that's missing (colisage == 1), never a second multiplier to layer on
    # top of a colisage that's already doing its job.
    count_already_in_colisage = count_value is not None and (colisage != 1 or count_value == qty)
    debug["count_already_in_colisage"] = count_already_in_colisage

    if size_value is not None:
        canonical = VOLUME_UNITS.get(size_unit) or WEIGHT_UNITS.get(size_unit)
        size_in_base = size_value * canonical
        # A bulk container's own size sometimes ends up printed as colisage
        # instead of a real pack count (a single 10L jerrycan invoiced as
        # colisage=10, not 10 jerrycans) - when colisage numerically matches
        # the size found in the name, quantity already IS the total
        # volume/weight bought (colisage x containers-bought), so trust it
        # directly instead of ALSO multiplying by the size - exactly the
        # same principle as trusting a supplier-printed total_volume in
        # Case 0, just arriving via colisage instead. Confirmed against
        # every real product in this database with this shape (bulk
        # cleaning liquids, cooking oil) before generalising this check.
        if colisage != 1 and size_value == int(size_value) and colisage == int(size_value):
            unit = "KG" if size_unit not in VOLUME_UNITS else "L"
            debug["colisage_matches_size"] = f"colisage={colisage} == {size_value}{size_unit}"
            return QuantityGuess(
                unit, Decimal("1"), "high",
                f"colisage {colisage} = taille {size_value}{size_unit} (déjà reflété)",
                debug=debug, suggested_stock_unit=unit,
            )
        if count_value is not None and not count_already_in_colisage:
            # size-per-item AND a genuine extra count neither invoice line nor
            # colisage reflects (e.g. 100 individually-wrapped 10g portions in
            # one box) - stock_equivalent is the count, matching how this is
            # already handled elsewhere in the app (the stock item is tracked
            # by portion count, not by total weight).
            return QuantityGuess(
                "UNIT",
                Decimal(count_value),
                "medium",
                f"{size_value}{size_unit} x {count_value}, compte non reflété par colisage",
                approx,
                debug,
            )
        is_volume = size_unit in VOLUME_UNITS
        if not assume_volume_tracked and is_volume and (size_in_base <= SMALL_FORMAT_VOLUME_THRESHOLD or is_container):
            reason = "contenant (verre/distributeur)" if is_container else f"{size_value}{size_unit} ≤ 33cl"
            debug["small_format"] = reason
            return QuantityGuess(
                "UNIT", Decimal("1"), "high", f"vendu à l'unité ({reason})", approx, debug
            )
        return QuantityGuess(
            "UNIT",
            size_in_base,
            "high" if not approx else "medium",
            f"taille {size_value}{size_unit} par article" + (" (environ)" if approx else ""),
            approx,
            debug,
            suggested_stock_unit="KG" if not is_volume else "L",
        )

    if count_value is not None:
        if count_already_in_colisage:
            return QuantityGuess("UNIT", Decimal("1"), "high", "compte déjà reflété par colisage", approx, debug)
        return QuantityGuess("UNIT", Decimal(count_value), "medium", f"compte {count_value} tiré du nom", approx, debug)

    return QuantityGuess("UNIT", Decimal("1"), "low", "aucun indice de quantité trouvé dans le nom", approx, debug)


def extract_quantity_for_product(
    product, force_unit_count: bool = False, assume_volume_tracked: bool = False
) -> QuantityGuess:
    """Convenience wrapper: pulls colisage/quantity/total_volume from the
    product's first invoice line - the same "representative example" the
    review queue and the matching rules elsewhere already use. See
    extract_quantity() for what the two override flags do."""
    line = product.invoice_lines.first()
    if line is None:
        return QuantityGuess("UNIT", Decimal("1"), "low", "aucune ligne de facture")
    return extract_quantity(
        product.raw_name,
        line.colisage,
        line.quantity,
        line.total_volume,
        force_unit_count=force_unit_count,
        assume_volume_tracked=assume_volume_tracked,
    )


def strip_size_and_count_tokens(raw_name: str) -> str:
    """Strips every size/count/dimension marker extract_quantity() itself
    would recognise, plus the house-brand prefix - e.g. "MPRO PH 2PLIS 200F
    24RLX" -> "PH 2PLIS". Used to build a stock item name for a product no
    rule recognises, so the size that already informed the auto-filled
    quantity doesn't also show up as noise in the suggested name.

    Order-independent unlike extract_quantity's careful count-vs-size
    disambiguation: nothing here interprets what's found, only removes it,
    so every pattern can just be masked out in one pass.
    """
    upper = raw_name.strip().upper()
    name = HOUSE_BRAND_PREFIX.sub("", upper)
    had_prefix = name != upper
    name = re.sub(r"(?<=[A-Z0-9])ENV", " ENV ", name)

    # SIZE_X_COUNT/COUNT_X_SIZE, then DIMENSION/LENGTH, then the two trailing-
    # count checks, THEN size-unit/can-format - same order extract_quantity
    # itself uses, and for the same reason: TRAILING_FUSED_COUNT_RE has to
    # see the string's true original end before SIZE_UNIT_RE can mask a real
    # trailing size like "1.5L" and leave "7UP" wrongly looking like a fused
    # count sitting at the (now-fake) end instead - which emptied the name
    # entirely for "7UP 1.5L" before this was caught.
    for pattern in (SIZE_X_COUNT_RE, COUNT_X_SIZE_RE, DIMENSION_RE, LENGTH_RE):
        for m in list(pattern.finditer(name)):
            name = _mask(name, m)

    m = TRAILING_FUSED_COUNT_RE.search(name)
    if m and m.group(2) not in KNOWN_UNIT_WORDS:
        name = _mask(name, m)

    for pattern in (TRAILING_COUNT_RE, CAN_FORMAT_RE, SIZE_UNIT_RE, MID_COUNT_RE, ENV_RE):
        for m in list(pattern.finditer(name)):
            name = _mask(name, m)

    # A fused leading count ("100SAC") is only trusted once a house-brand
    # prefix was actually stripped - otherwise a fused leading number is as
    # likely a brand name that starts with a digit ("7UP") as a real count,
    # exactly like LEADING_COUNT_RE's own comment explains; without a prefix,
    # require the space real counts are spaced-out with ("100 SERV"). Same
    # >=1000 "that's a brand name, not a count" guard as extract_quantity too
    # (a bare "1664" at the start is Kronenbourg 1664, not a count of 1664).
    pattern = LEADING_COUNT_RE if had_prefix else LEADING_COUNT_SPACED_RE
    m = pattern.match(name)
    if m and int(m.group(1)) < 1000:
        name = _mask(name, m)

    # Stray punctuation/separators left dangling once whatever they were
    # attached to got masked out ("SOMETHING, 33CL" -> "SOMETHING,").
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^[,.\-/]+\s*|\s*[,.\-/]+$", "", name)
    return re.sub(r"\s+", " ", name).strip()
