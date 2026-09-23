"""La page « Marges » - les trois margins, drawn over one « du … au … ».

The view does no arithmetic of its own: `computation.margins_for` answers,
and everything here is about saying WHICH question each figure answers and
what it is worth. Three decisions shape the page.

**A period is chosen, never assumed.** With no dates typed the page shows the
last twelve months and says so on screen, because an all-time margin mixes
three years of purchase prices with three years of selling prices and means
very little; « depuis le début » is one click away and keeps the dates it was
clicked from, the way the stock page's panels do.

**A margin is never printed without its coverage.** Only part of what the
till sells has a recipe behind it, and revenue with no cost reads as a 100 %
margin. So every figure built on the recipes carries how much of its money is
actually costed, in a number AND in words - a category reading 90 % on a third
of its units, and the number alone is a fiction.

**A gap is said at the top, not absorbed.** Days whose money was never read,
money with no VAT rate, invoices with no date: each is named where it can be
acted on, rather than quietly making the margin look better or worse.

**What is left out of the real margin is part of the address**, like the
period (`?sans=`, repeated): a view, not a setting, so two tabs can hold two
questions and a bookmark keeps its own. Every link and form of the page
carries it the way it carries `du`/`au`, built here with `urlencode`.

**« Compter dans la marge produits » is ticked from here too**, a whole
category at a time (`count_articles`). That one IS a setting - the article's
own field, the same box as on its form - so it is a POST, and it answers
where it was asked: back on the page, the period and the selection kept, on
the panel, with what changed said there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.messages import get_messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlencode

from common import DateRange, date_range, is_id
from inventory.models import StockType
from recipes.models import RecipeIngredient

from .computation import NO_CATEGORY, Exclusion, Slice, known_left_out, margins_for

HUNDRED = Decimal("100")

#: « Depuis le début » - all of the history, whatever dates are in the URL.
#: Named like the stock page's panels (`tout=1`), which mean the same thing:
#: everything, and remember what was asked so it can be offered back.
ALL_PARAM = "tout"

#: What the second real margin leaves out, one key per thing
#: (`computation.CHARGES_KEY`, `category_key`…), repeated.
LEFT_OUT_PARAM = "sans"
#: What « Recalculer » sends. A checkbox that is not ticked sends NOTHING, so
#: « left out » cannot be read off what came back alone: every row the table
#: showed sends its key under `montre`, and the boxes still ticked send it
#: again under `garder`. Left out is shown and not kept - and a key the form
#: never showed (nothing in the window, a newer invoice since) keeps the
#: state it had.
SHOWN_PARAM = "montre"
KEPT_PARAM = "garder"

#: « Articles comptés dans la marge produits »: the panel's anchor, and the
#: tag its messages carry so the page says them there and not at the top -
#: the redirect lands on the panel, two screens under the page's head.
PANEL = "articles-comptes"
#: What a category's form posts (`count_articles`). The category by its name
#: as stored - it is a free string on the article, the blank one included -
#: every article the form SHOWED, and the boxes still ticked: an unticked box
#: sends nothing, so « unticked » can only be read as shown and not ticked.
CATEGORY_FIELD = "categorie"
SHOWN_FIELD = "affiche"
TICKED_FIELD = "coche"
#: The boxes the form DREW ticked: what was changed on the page is what a
#: post changes, and nothing a stale page still shows the old state of.
DRAWN_TICKED_FIELD = "etait"
ACTION_FIELD = "action"
TICK_ALL = "cocher"
UNTICK_ALL = "decocher"
SAVE = "enregistrer"
#: How many names a message lists before it says « et N autres ».
NAMED_AT_MOST = 5

#: The default period. The same 365 days « Produits & charges » falls back on
#: for its charges, so the two pages cannot disagree about what « les douze
#: derniers mois » means.
DEFAULT_DAYS = 365


def _last_twelve_months(today=None) -> DateRange:
    today = today or timezone.localdate()
    return DateRange(today - timedelta(days=DEFAULT_DAYS), today)


def _dates(window: DateRange) -> str:
    """« du 01/02/2026 au 28/02/2026 » - either end alone is a window a person
    asks for, and « du 01/02/2026 au » reads as a page that lost half of its
    own question."""
    start = window.start.strftime("%d/%m/%Y") if window.start else ""
    end = window.end.strftime("%d/%m/%Y") if window.end else ""
    if start and end:
        return f"du {start} au {end}"
    if start:
        return f"depuis le {start}"
    if end:
        return f"jusqu'au {end}"
    return ""


def _percent(share: Decimal | None) -> Decimal | None:
    """A 0-1 share as a percentage, keeping None as None: nothing to state is
    not 0 %, which on this page reads as « everything was lost »."""
    return None if share is None else share * HUNDRED


@dataclass(frozen=True)
class SliceRow:
    """One line of the two category tables: the slice, and what the page has
    to say beside its margin.

    The note is the point of this class. A margin of 90 % on a third of
    the money costed is not a 90 % margin, and a percentage sitting alone in a
    column will be read as one - so the words travel with the figure, in the
    same cell, and a slice with nothing costed has no margin at all.

    **The share is the MONEY**, like the headline « Part chiffrée », with the
    units said in the same cell. The two were the money and the units under
    one label, 33 points apart on one screen: a category selling 100 cafés at
    2 € with no recipe beside 100 cocktails at 10 € with one is half its
    units and five sixths of its money.
    """

    slice: Slice

    @property
    def name(self) -> str:
        return self.slice.name

    @property
    def coverage_percent(self) -> Decimal | None:
        return _percent(self.slice.revenue_coverage)

    @property
    def units_note(self) -> str:
        """The unit count, since the share beside it is the money. Dropped,
        it would be nowhere on the page - and four planches at 18 € are a
        different problem from forty cafés at 2 €."""
        if not self.slice.units:
            return ""
        return f"{self.slice.costed_units} unités sur {self.slice.units}"

    @property
    def note(self) -> str:
        if not self.slice.units and not self.slice.revenue.ttc:
            return "aucune vente"
        # Money with no VAT rate has no HT, so the slice prints its cost
        # against a revenue of 0,00 € - a loss, and under the unit counts it
        # read « tout est chiffré ». This is the only thing that says why.
        if self.slice.revenue_without_rate_ttc:
            amount = f"{self.slice.revenue_without_rate_ttc:.2f}"
            return f"{amount} € encaissés sans taux de TVA : pas de HT, marge faussée"
        if not self.slice.is_costed:
            return "aucune recette : pas de marge calculable"
        # Read off the unit counts, a row whose units net to what is costed
        # (one sold, one taken back) said « tout est chiffré » while money
        # sat there with no cost behind it. The money is what the margin is.
        if self.slice.revenue_uncosted.ht or self.slice.revenue_uncosted.ttc:
            return "le reste n'a pas de recette : marge surestimée"
        return "tout est chiffré"


def _total_row(slices: list[Slice]) -> SliceRow | None:
    """The two tables' own total - the page claimed they footed alike and
    showed neither foot. Built from the slices themselves rather than from
    the report: the report's cogs also holds the recipes sold off the till,
    which are in no category, and a total that did not add up to its own
    column is worse than none."""
    if not slices:
        return None
    total = Slice(name="Total")
    for slice_ in slices:
        total.revenue += slice_.revenue
        total.revenue_uncosted += slice_.revenue_uncosted
        total.revenue_without_rate_ttc += slice_.revenue_without_rate_ttc
        total.cost_ht_low += slice_.cost_ht_low
        total.cost_ht_high += slice_.cost_ht_high
        total.units += slice_.units
        total.costed_units += slice_.costed_units
    return SliceRow(total)


@dataclass(frozen=True)
class ExclusionRow:
    """One thing left out, and the same page with it put back."""

    exclusion: Exclusion
    put_back_url: str


def margins_home(request):
    asked = date_range(request)
    showing_all = request.GET.get(ALL_PARAM) == "1"
    if SHOWN_PARAM in request.GET:
        # « Recalculer »: answered with the clean address - `sans` alone, in
        # the page's own spelling - rather than a page drawn under a URL
        # holding every key of the table twice.
        left_out = known_left_out(_unticked(request))
        return redirect(_page_url(asked, showing_all=showing_all, left_out=left_out))

    # The three periods this page can be on, in the order they win. « Tout
    # l'historique » is a named period like Banque's month or Produits &
    # charges' inventaire: it takes the window whole rather than being
    # crossed with the dates, which stay in the URL only to be offered back.
    window = DateRange() if showing_all else (asked or _last_twelve_months())
    report = margins_for(window, request.GET.getlist(LEFT_OUT_PARAM))
    # What the page carries on: the keys it understood, never the ones it
    # dropped, so a garbled key does not travel from link to link.
    left_out = [exclusion.key for exclusion in report.exclusions]
    # A key held inside another one left out (an article of « Matériel »,
    # « Matériel » being out too) is not named: it says nothing more.
    named = [exclusion for exclusion in report.exclusions if exclusion.within is None]
    here_url = _page_url(asked, showing_all=showing_all, left_out=left_out)
    page_messages, panel_messages = _messages_by_place(request)

    return render(
        request,
        "margins/page.html",
        {
            "report": report,
            # What the reader typed, for the inputs; `window` is what the
            # figures are over. They differ on the default period and under
            # « depuis le début », and the page says which it is showing.
            "date_window": asked,
            "window": window,
            "window_dates": _dates(window),
            "asked_dates": _dates(asked),
            "showing_all": showing_all,
            "is_default": not showing_all and not asked,
            "all_url": _page_url(asked, showing_all=True, left_out=left_out),
            "period_url": _page_url(asked, showing_all=False, left_out=left_out),
            # « Effacer » clears the dates, not the selection.
            "clear_url": _page_url(DateRange(), showing_all=False, left_out=left_out),
            # This very page, for a form that answers back to it (`next`).
            "here_url": here_url,
            # The panel's forms post there and come back here, on the panel.
            "count_articles_url": reverse("margins:count_articles"),
            "articles_next": f"{here_url}#{PANEL}",
            "panel": PANEL,
            # The base template's messages, split: the panel's own are said
            # in the panel, where the redirect lands; any other at the top.
            "page_messages": page_messages,
            "panel_messages": panel_messages,
            # « Tout remettre »: the same period, nothing left out.
            "reset_url": _page_url(asked, showing_all=showing_all),
            "left_out": left_out,
            "left_out_names": ", ".join(exclusion.name for exclusion in named),
            "exclusions": [
                ExclusionRow(
                    exclusion,
                    _page_url(
                        asked,
                        showing_all=showing_all,
                        # Its own key, and what the label hid under it: named
                        # « Matériel » alone, « remettre » left Perceuse (unticked
                        # inside it) out, and only then named it.
                        left_out=[
                            other.key
                            for other in report.exclusions
                            if other.key != exclusion.key and other.within_key != exclusion.key
                        ],
                    ),
                )
                for exclusion in named
            ],
            "page_path": reverse("margins:margins_home"),
            # « Recalculer » carries the period and what the table does not
            # show, as hidden fields: a GET form sends only its own fields.
            "selection_fields": [
                *asked.parameters.items(),
                *([(ALL_PARAM, "1")] if showing_all else []),
                *((LEFT_OUT_PARAM, key) for key in left_out),
            ],
            # Nothing was rung up and nothing was sold off the till either,
            # so there is no income to compare anything to. The figures are
            # still drawn - nil reads as nil, where a row that disappears
            # reads as a broken page - and the next step is offered beside
            # them, which is where the sales come from. The till's MONEY is
            # asked too: a day that only refunded is stored at quantity 0
            # with a negative amount, and « Aucune vente enregistrée » sat
            # over a page showing -15,00 € of takings.
            "no_sales": (
                not report.units
                and not report.revenue_till.ttc
                and not report.revenue_documents.ttc
            ),
            "categories": [SliceRow(slice_) for slice_ in report.by_category],
            "typologies": [SliceRow(slice_) for slice_ in report.by_typology],
            "categories_total": _total_row(report.by_category),
            "typologies_total": _total_row(report.by_typology),
            "coverage_percent": _percent(report.coverage),
            "revenue_coverage_percent": _percent(report.revenue_coverage),
            # Where a reader goes to do something about what the page says,
            # each carrying the period it is showing: a list opening on
            # another period than the figures it was reached from is how two
            # screens come to disagree with nothing saying why.
            "documents_url": _elsewhere("invoices:invoice_list", window),
            # « Leur donner une date » had nowhere to click. Achats' own
            # « sans date » list drops the window on purpose (a row with no
            # date is in no window), so this link carries none either.
            "undated_url": f"{reverse('invoices:invoice_list')}?sans_date=1",
            "purchases_url": _elsewhere("inventory:stock_list", window),
            "sales_url": _elsewhere("recipes:sales_list", window),
            "to_link_url": reverse("recipes:pos_product_list"),
            "sales_import_url": reverse("recipes:sales_import"),
        },
    )


def _unticked(request) -> list[str]:
    """What « Recalculer » leaves out: the rows the form showed and did not
    send back ticked, after what was left out already and not on the form.

    Read as « shown and not kept », never as « not sent »: an unticked box
    sends nothing, and so does a row a stale page never had - a newer
    invoice's article read as unticked would drop out of the margin with
    nobody having touched it. A `garder` for a row the form never showed
    changes nothing either.

    In the order they were ASKED, what is newly left out after: in table
    order, a « Recalculer » that changed nothing turned « sans : Matériel,
    Rhum » into « sans : Rhum, Matériel »."""
    shown = request.GET.getlist(SHOWN_PARAM)
    kept = set(request.GET.getlist(KEPT_PARAM))
    on_the_form = set(shown)
    before = request.GET.getlist(LEFT_OUT_PARAM)
    unticked = {key for key in shown if key not in kept}
    still_out = [key for key in before if key not in on_the_form or key in unticked]
    return still_out + [key for key in shown if key in unticked and key not in before]


def _page_url(window: DateRange, *, showing_all: bool, left_out: list[str] | tuple = ()) -> str:
    """This page with the period and what is left out kept. Built here
    rather than pasted together in the template, which is exactly where a
    parameter gets forgotten - and the window falling off the page on a
    click changes every figure on it with nothing on screen to say why.

    A list of pairs, because `sans` repeats; `urlencode` takes each key
    through exactly, « Matériel », spaces and the blank category's empty
    name included."""
    parameters = list(window.parameters.items())
    if showing_all:
        parameters.append((ALL_PARAM, "1"))
    parameters += [(LEFT_OUT_PARAM, key) for key in left_out]
    url = reverse("margins:margins_home")
    return f"{url}?{urlencode(parameters)}" if parameters else url


def _elsewhere(url_name: str, window: DateRange) -> str:
    """Another page of the app, over the period this one is showing."""
    url = reverse(url_name)
    return f"{url}?{urlencode(window.parameters)}" if window else url


def _messages_by_place(request) -> tuple[list, list]:
    """(the messages said at the top of the page, the ones said in the
    panel). Read once here, which also marks them said."""
    page, panel = [], []
    for message in get_messages(request):
        (panel if PANEL in (message.extra_tags or "").split() else page).append(message)
    return page, panel


# -- « Articles comptés dans la marge produits » -----------------------------


def count_articles(request):
    """Tick or untick « compter dans la marge produits » for the articles of
    ONE category - the whole of it (« Tout cocher » / « Tout décocher »), or
    its boxes one by one (« Enregistrer »).

    **A post only ever touches the articles its own form showed**, and of
    those only the ones still in the category it names:

    * the whole-category buttons touch every article the category holds
      TODAY - one classified into it after the page was drawn included, which
      is what « the whole category » means; one classified later still
      arrives unticked, and the panel reads « 31 sur 32 »;
    * « Enregistrer » changes the shown boxes the person changed: drawn
      unticked and sent back ticked, drawn ticked (`etait`) and not sent
      back - a box left as drawn keeps whatever another tab made of it since.
      An unticked box sends nothing at all, so
      « not sent » alone cannot mean « unticked »: an article reclassified
      into the category since the page was drawn was never on this form and
      keeps its state, and an id of another category - tampered, or
      reclassified the other way - is never changed by it.

    Ids go through `common.is_id`; an id that is none, an article gone, a
    category nobody carries any more are each a message, never a 500. What
    changed is said in the panel, where the redirect lands (`next`, checked
    like every other one).
    """
    back = _back(request)
    if request.method != "POST":
        return redirect(back)

    if CATEGORY_FIELD not in request.POST:
        messages.error(request, "Formulaire incomplet, sans catégorie : rien n'a été modifié.", extra_tags=PANEL)
        return redirect(back)
    category = request.POST[CATEGORY_FIELD]
    # Its articles as they are NOW - the category is a free string on the
    # article, so this is the only definition of what it holds.
    in_category = dict(StockType.objects.filter(category=category).values_list("pk", "count_in_products_margin"))
    if not in_category:
        messages.error(
            request,
            f"Aucun article n'est dans la catégorie « {category or NO_CATEGORY} » : rien n'a été modifié. "
            "Ses articles ont peut-être changé de catégorie depuis que la page a été affichée.",
            extra_tags=PANEL,
        )
        return redirect(back)

    action = request.POST.get(ACTION_FIELD)
    if action == TICK_ALL:
        tick, untick = set(in_category), set()
    elif action == UNTICK_ALL:
        tick, untick = set(), set(in_category)
    elif action == SAVE:
        tick, untick = _read_the_boxes(request, category, set(in_category))
    else:
        messages.error(request, "Action inconnue : rien n'a été modifié.", extra_tags=PANEL)
        return redirect(back)

    ticked_now = sorted(pk for pk in tick if not in_category[pk])
    unticked_now = sorted(pk for pk in untick if in_category[pk])
    with transaction.atomic():
        if ticked_now:
            StockType.objects.filter(pk__in=ticked_now).update(count_in_products_margin=True)
        if unticked_now:
            StockType.objects.filter(pk__in=unticked_now).update(count_in_products_margin=False)

    if ticked_now:
        messages.success(
            request,
            f"{_articles_of(len(ticked_now), category)} {_agreed(len(ticked_now), 'compté')} dans la marge produits.",
            extra_tags=PANEL,
        )
    if unticked_now:
        messages.success(
            request,
            f"{_articles_of(len(unticked_now), category)} {_agreed(len(unticked_now), 'retiré')} de la marge produits.",
            extra_tags=PANEL,
        )
    if not ticked_now and not unticked_now:
        now = sum(1 for flag in in_category.values() if flag)
        state = "aucun" if not now else "tous" if now == len(in_category) else f"{now} sur {len(in_category)}"
        messages.info(
            request,
            f"{category or NO_CATEGORY} : rien n'a changé ({state} {_agreed(now, 'compté')} dans la marge produits).",
            extra_tags=PANEL,
        )
    _warn_counted_twice(request, ticked_now)
    return redirect(back)


def _read_the_boxes(request, category: str, in_category: set[int]) -> tuple[set[int], set[int]]:
    """(to tick, to untick) from what « Enregistrer » sent: the boxes the
    person CHANGED - drawn unticked and sent back ticked, drawn ticked and
    not sent back - of this category only. What is dropped is said.

    Changed, not merely « shown and not ticked »: the form also says how it
    drew each box (`etait`, the ones drawn ticked). Read off the boxes alone,
    a page left open while another tab ticked Gobelets unticked it again on
    its next « Enregistrer » - the person never touched that box, and never
    saw it ticked. A box left as it was drawn is left as it is now."""
    posted = request.POST.getlist(SHOWN_FIELD) + request.POST.getlist(TICKED_FIELD)
    garbled = {value for value in posted if not is_id(value)}
    shown = {int(value) for value in request.POST.getlist(SHOWN_FIELD) if is_id(value)}
    kept = {int(value) for value in request.POST.getlist(TICKED_FIELD) if is_id(value)}
    drawn_ticked = {int(value) for value in request.POST.getlist(DRAWN_TICKED_FIELD) if is_id(value)}

    # Gone since the page was drawn, reclassified into another category, or
    # an id this form never held: left exactly as it is, and counted.
    outside = (shown | kept) - in_category
    if outside:
        count = len(outside)
        messages.warning(
            request,
            (
                f"1 case ignorée : l'article n'existe plus, ou n'est plus {_in(category)} depuis que la page "
                "a été affichée. Rien n'a été changé pour lui."
                if count == 1
                else f"{count} cases ignorées : ces articles n'existent plus, ou ne sont plus {_in(category)} "
                "depuis que la page a été affichée. Rien n'a été changé pour eux."
            ),
            extra_tags=PANEL,
        )
    if garbled:
        count = len(garbled)
        messages.warning(
            request,
            "1 case illisible ignorée." if count == 1 else f"{count} cases illisibles ignorées.",
            extra_tags=PANEL,
        )
    # A box sent back ticked that the form never showed changes nothing: the
    # form is what the person saw.
    shown &= in_category
    return (shown & kept) - drawn_ticked, (shown - kept) & drawn_ticked


def _warn_counted_twice(request, ticked_now: list[int]) -> None:
    """Said the moment it happens: an article just ticked that a recipe uses
    is now paid for twice - once as the recipe consumes it, once as it is
    bought. « Tout cocher » on the spirits does exactly that to every one of
    them, and the page's own warning is two screens above the panel."""
    if not ticked_now:
        return
    names = sorted(
        set(
            RecipeIngredient.objects.filter(stock_type_id__in=ticked_now)
            .order_by()
            .values_list("stock_type__name", flat=True)
        )
    )
    if not names:
        return
    listed = ", ".join(names[:NAMED_AT_MOST])
    if len(names) > NAMED_AT_MOST:
        listed += f" et {len(names) - NAMED_AT_MOST} autres"
    if len(names) == 1:
        text = (
            f"Compté deux fois désormais : {listed} sert dans une recette, qui compte déjà ce qu'elle en "
            "consomme. Décochez-le, ou retirez-le de la recette."
        )
    else:
        text = (
            f"Comptés deux fois désormais : {listed} servent dans des recettes, qui comptent déjà ce "
            "qu'elles en consomment. Décochez-les, ou retirez-les de leurs recettes."
        )
    messages.warning(request, text, extra_tags=PANEL)


def _articles_of(count: int, category: str) -> str:
    """« 12 articles de Matériel »."""
    noun = "article" if count == 1 else "articles"
    return f"{count} {noun} de {category}" if category else f"{count} {noun} de « {NO_CATEGORY} »"


def _in(category: str) -> str:
    return f"dans {category}" if category else f"dans « {NO_CATEGORY} »"


def _agreed(count: int, participle: str) -> str:
    """« compté », « comptés » - aucun and 1 take the singular."""
    return participle if count <= 1 else f"{participle}s"


def _back(request) -> str:
    """Where a post answers: its `next` when it is this site's - the page as
    it was read, with its period and its selection - and the panel of the
    bare page otherwise. Checked like `bank.views._back`: a `next` is
    something a form carries, and anything can be put in one."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return f"{reverse('margins:margins_home')}#{PANEL}"
