import json
import logging
import math
import posixpath
import secrets

# Not used here any more: the tests patch the import's thread as
# `recipes.views.threading.Thread`, which is the threading module's own
# (recipes/importing.py starts it).
import threading  # noqa: F401
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.messages import get_messages
from django.db import transaction
from django.db.models import ProtectedError, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import escape
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_safe

from accounts.access import access_of
from accounts.views import DOWNLOAD_PARAM, file_response, open_stored
from common import PIE_COLORS, error_for_page, format_money, is_id, plain_number, posted_digest
from invoices.einvoice import EInvoiceError
from invoices.filenames import sale_download_name

from .forms import (
    CONSUMED_CLEARED,
    CONSUMED_ZERO,
    FILE_AGAIN,
    KNOWN_CUSTOMERS_LIST,
    MANUAL_SALE_SOURCE,
    NOTHING_SOLD,
    ManualSaleForm,
    RecipeForm,
    RecipeIngredientFormSet,
    SaleDocumentForm,
    SaleDocumentLineFormSet,
    SaleDocumentLineFormSetNew,
    SaleEInvoiceHeaderForm,
    SaleTiesForm,
    ingredient_categories,
    ingredient_unit_map,
)
from .importing import active_import, start_sales_import
from .integration import refusal, till_allowed
from .links import LinkError, link, set_aside
from .menu import (
    SALE_CARD,
    kept_fields,
    kept_query,
    pending_count,
    render_menu,
    sales_list_url,
    url_for_each,
    with_suggestion,
)
from .models import (
    SALE_FILES_FOLDER,
    PosProduct,
    Recipe,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
    SalesImportJob,
    read_unit_costs,
    variation_scope,
)
from .sale_documents import DELETE_QUESTION, counted_twice, deposit_doubts, known_customers
from .sale_einvoice import MAX_SALE_LINES
from .sale_files import (
    DocumentGone,
    SaleFileRefused,
    delete_document,
    read_einvoice_upload,
    save_typed,
    siren_text,
    staged,
    typed_file_problem,
)
from .sale_lines import proposals
from .sale_payments import payment_context, read_links

logger = logging.getLogger(__name__)

#: Said when a sales import is already running (by hand or automatic).
ALREADY_RUNNING = "Une récupération est déjà en cours."


def _existing_categories():
    return Recipe.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")


def _build_ingredient_pie_svg(breakdown: list[dict]) -> str:
    """Each ingredient's share of a recipe's cost, as an inline SVG pie.

    Built from the exact same breakdown cost_ht() sums, so the chart and the
    displayed total can never disagree. Empty string when there's no cost to
    show a proportion of.

    Hand-rolled for the same reason as the price chart (see
    inventory/views.py::_build_price_history_svg): a charting library's only
    real contribution here would be the tooltip, and static/js/charts.js does
    that for both charts at once.

    The result is rendered with |safe, so every name that goes into it is
    escaped here - those come from invoice text and from whatever was typed
    on the stock page, neither of which is trustworthy markup.
    """
    total = sum((entry["cost_ht"] for entry in breakdown), start=Decimal("0"))
    non_zero = [entry for entry in breakdown if entry["cost_ht"] > 0]
    if not non_zero or not total:
        return ""

    size, radius = 220, 100
    cx = cy = size / 2

    slices, legend = [], []
    start_angle = -math.pi / 2  # 12 o'clock
    for index, entry in enumerate(non_zero):
        fraction = float(entry["cost_ht"] / total)
        color = PIE_COLORS[index % len(PIE_COLORS)]
        name = escape(entry.get("name") or entry["ingredient"].source_name)
        # The tooltip's text (static/js/charts.js shows it as it is), so its
        # amount is grouped like every other one a person reads.
        value = f"{format_money(entry['cost_ht'])} € · {fraction * 100:.1f} %"
        common = f'data-index="{index}" data-label="{name}" data-value="{value}" data-color="{color}"'

        if len(non_zero) == 1:
            # A single 100% slice can't be drawn as an arc - the start and end
            # points coincide and the path collapses to nothing.
            slices.append(f'<circle class="chart-slice" cx="{cx}" cy="{cy}" r="{radius}" fill="{color}" {common} />')
        else:
            end_angle = start_angle + fraction * 2 * math.pi
            x1, y1 = cx + radius * math.cos(start_angle), cy + radius * math.sin(start_angle)
            x2, y2 = cx + radius * math.cos(end_angle), cy + radius * math.sin(end_angle)
            large_arc = 1 if fraction > 0.5 else 0
            slices.append(
                f'<path class="chart-slice" d="M{cx},{cy} L{x1:.1f},{y1:.1f} '
                f'A{radius},{radius} 0 {large_arc} 1 {x2:.1f},{y2:.1f} Z" fill="{color}" {common} />'
            )
            start_angle = end_angle

        legend.append(
            f'<span class="chart-legend-item" data-legend-for="{index}">'
            f'<span class="swatch" style="background:{color};"></span>{name}'
            f' <span class="muted">{fraction * 100:.1f} %</span></span>'
        )

    return (
        f'<div class="chart chart-pie" data-chart="pie" style="max-width:260px;">'
        f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="Répartition du coût">'
        f"{''.join(slices)}</svg>"
        f'<div class="chart-tooltip" data-chart-tooltip></div>'
        f'<div class="chart-legend">{"".join(legend)}</div>'
        f"</div>"
    )


def recipe_list(request):
    """ "Recettes & ventes", on the recipes."""
    return render_menu(request, "recipes")


# Beyond this many variations the picker stops listing them individually and
# offers one dropdown per choice group instead. Well below the point where
# rendering would actually struggle - a list of a few hundred near-identical
# option labels has stopped being useful to read long before that.
MAX_LISTED_VARIATIONS = 60


def recipe_detail(request, pk):
    recipe = get_object_or_404(Recipe, pk=pk)
    ingredients = list(
        recipe.ingredients.select_related("stock_type", "sub_recipe").prefetch_related("stock_type__movements")
    )
    # One scope around the whole page: the summary, the chosen variation and
    # the picker labels all ask the same sub-recipes the same questions.
    with variation_scope():
        return _render_recipe_detail(request, recipe, ingredients)


def _render_recipe_detail(request, recipe, ingredients):
    groups = recipe.choice_groups(ingredients)
    summary = recipe.summary(ingredients)

    # Which option is picked in each group, from ?v=0.2.1 - one index per
    # group. Out-of-range or malformed values fall back to the first option
    # (see variation_for) rather than 404ing, so an old bookmark still works
    # after the recipe has been edited.
    selection = [_to_int(part) for part in (request.GET.get("v") or "").split(".") if part != ""]
    variation = recipe.variation_for(selection, ingredients)

    listed_variations = None
    if 1 < summary["variation_count"] <= MAX_LISTED_VARIATIONS:
        listed_variations = [
            {"value": _selection_key(indices), "name": name}
            for indices, name in recipe.variation_selections(ingredients)
        ]

    normalised = _normalised_selection(selection, groups)
    return render(
        request,
        "recipes/recipe_detail.html",
        {
            "recipe": recipe,
            "summary": summary,
            "variation": variation,
            # One entry per choice group, for the per-group pickers used
            # when there are too many variations to list individually.
            "group_pickers": [
                {
                    "position": position,
                    "selected": normalised[position],
                    "options": [
                        {"index": index, "name": ingredient.source_name} for index, ingredient in enumerate(group)
                    ],
                }
                for position, group in enumerate(groups)
                if len(group) > 1
            ],
            "listed_variations": listed_variations,
            "selection_key": _selection_key(normalised),
            "till_products": list(recipe.pos_products.order_by("name")),
            "units_sold": recipe.sales.aggregate(units=Sum("quantity"))["units"] or 0,
            "pie_svg": _build_ingredient_pie_svg(variation["breakdown"]) if variation else "",
        },
    )


def _to_int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def _normalised_selection(selection: list[int], groups) -> list[int]:
    selection = list(selection) + [0] * (len(groups) - len(selection))
    return [
        selection[position] if 0 <= selection[position] < len(group) else 0 for position, group in enumerate(groups)
    ]


def _selection_key(indices) -> str:
    """The ?v= value for one selection: one option index per group, e.g.
    "0.2.1". Positional rather than by ingredient id so a link stays short
    however many groups there are."""
    return ".".join(str(index) for index in indices)


def _recipe_form_view(request, recipe, for_product=None):
    """Write a recipe - and link it to what the till sells it as. Created
    for a till product (`for_product`), it goes back to the till products to
    link once saved."""
    initial = {"pos_products": [for_product.pk]} if for_product is not None else None
    if request.method == "POST":
        form = RecipeForm(request.POST, instance=recipe)
        formset = RecipeIngredientFormSet(
            request.POST, instance=recipe, form_kwargs={"parent_recipe": recipe if recipe.pk else None}
        )
        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                recipe = form.save()
                formset.save()
                linked = _link_till_products(recipe, form.cleaned_data["pos_products"])
            messages.success(
                request,
                f"« {recipe.name} » enregistrée"
                + (f", vendue en caisse sous {', '.join(f'« {name} »' for name in linked)}." if linked else "."),
            )
            if for_product is not None:
                return redirect("recipes:pos_product_list")
            return redirect("recipes:recipe_detail", pk=recipe.pk)
    else:
        form = RecipeForm(instance=recipe, initial=initial)
        formset = RecipeIngredientFormSet(instance=recipe, form_kwargs={"parent_recipe": recipe if recipe.pk else None})
    return render(
        request,
        "recipes/recipe_form.html",
        {
            "form": form,
            "formset": formset,
            "recipe": recipe,
            "for_product": for_product,
            "existing_categories": _existing_categories(),
            "ingredient_units": ingredient_unit_map(),
            "ingredient_categories": ingredient_categories(),
        },
    )


def _link_till_products(recipe, wanted) -> list[str]:
    """Link the till products the form names to `recipe`, and take off it
    those it no longer names. Returns the names it is sold under."""
    wanted_ids = {product.pk for product in wanted}
    for product in recipe.pos_products.exclude(pk__in=wanted_ids).select_related("recipe"):
        set_aside(product, ignored=False)
    for product in wanted:
        if product.recipe_id != recipe.pk:
            link(product, recipe)
    return sorted(product.name for product in wanted)


def recipe_create(request):
    """A new recipe. `?caisse=<id>` writes it for that till product: named
    after it and linked to it on save. `?name=` prefills the name alone."""
    posted = request.GET.get("caisse", "")
    for_product = PosProduct.objects.filter(pk=posted, recipe__isnull=True).first() if is_id(posted) else None
    name = (request.GET.get("name") or (for_product.name if for_product else "")).strip()
    return _recipe_form_view(request, Recipe(name=name), for_product=for_product)


def recipe_update(request, pk):
    return _recipe_form_view(request, get_object_or_404(Recipe, pk=pk))


def recipe_delete(request, pk):
    if request.method != "POST":
        return redirect("recipes:recipe_list")
    recipe = get_object_or_404(Recipe, pk=pk)
    if recipe.used_in.exists():
        used_by = ", ".join(f'"{ri.recipe.name}"' for ri in recipe.used_in.select_related("recipe"))
        messages.error(request, f'Impossible de supprimer "{recipe.name}" : utilisée comme ingrédient dans {used_by}.')
        return redirect("recipes:recipe_detail", pk=pk)
    name = recipe.name
    try:
        with transaction.atomic():
            recipe.delete()
    except ProtectedError as exc:
        # A sale document's lines PROTECT their recipe: the document must
        # keep saying what was sold. Said as delete_stock_type says it.
        documents = {obj.document_id for obj in exc.protected_objects if isinstance(obj, SaleDocumentLine)}
        where = f"utilisée dans {len(documents)} document(s) de vente" if documents else "encore utilisée"
        messages.error(request, f'Impossible de supprimer "{name}" : {where}.')
        return redirect("recipes:recipe_detail", pk=pk)
    messages.success(request, f'"{name}" supprimée.')
    return redirect("recipes:recipe_list")


# --- Till (L'Addition) -----------------------------------------------------


def pos_product_list(request):
    """ "Recettes & ventes", on the till products still to link."""
    return render_menu(request, "to-link")


def pos_product_assign(request, pk):
    """Link one till product to a recipe, or set it aside - in place, from
    its row, which comes back saying what was done (with an undo).

    "Happy hour" is a MODIFIER on linking rather than an action of its own:
    it links to the same recipe, and additionally records this as the name
    the till uses during happy hour so both sets of sales land together.
    """
    if request.method != "POST":
        return redirect("recipes:pos_product_list")
    product = get_object_or_404(PosProduct.objects.select_related("recipe"), pk=pk)
    action = request.POST.get("action")
    outcome, error = None, None

    if action == "ignore":
        set_aside(product, ignored=True)
        outcome = "ignored"
        message = f"« {product.name} » ignoré."
    elif action == "reset":
        set_aside(product, ignored=False)
        message = f"« {product.name} » remis à traiter."
    elif action in ("link", "happy_hour"):
        # "happy_hour" is still accepted so an old bookmark or a half-submitted
        # form doesn't 400; the checkbox is what the page sends now.
        as_happy_hour = action == "happy_hour" or bool(request.POST.get("as_happy_hour"))
        posted = request.POST.get("recipe", "")
        recipe = Recipe.objects.filter(pk=posted).first() if is_id(posted) else None
        if recipe is None:
            error = "Choisissez une recette."
        else:
            try:
                link(product, recipe, happy_hour=as_happy_hour)
            except LinkError as exc:
                error = str(exc)
            else:
                outcome = "linked"
                message = (
                    f"« {product.name} » enregistré comme happy hour de « {recipe.name} »."
                    if as_happy_hour
                    else f"« {product.name} » lié à « {recipe.name} »."
                )
    else:
        error = "Action inconnue."

    if request.headers.get("HX-Request") == "true":
        recipes = list(Recipe.objects.order_by("name"))
        response = render(
            request,
            "recipes/_pos_row.html",
            {"product": with_suggestion(product, recipes), "recipes": recipes, "outcome": outcome, "error": error},
        )
        # The counts beside "À lier" follow (ui.js). Not as out-of-band
        # parts: beside a <tr>, htmx 1.9's parser moves them out of the row.
        response["HX-Trigger"] = json.dumps({"to-link-count": pending_count()})
        return response
    if error:
        messages.error(request, error)
    else:
        messages.success(request, message)
    return redirect("recipes:pos_product_list")


def sales_import(request):
    """The till import now sits on the sales tab."""
    return render_menu(request, "sales")


def trigger_sales_import(request):
    if request.method != "POST":
        return redirect(sales_list_url(request))
    # The server's L'Addition account is the owner's (recipes/integration.py):
    # the tab draws no form elsewhere, and a post from a page drawn before, or
    # crafted, is refused here.
    if not till_allowed():
        messages.error(request, refusal())
        return redirect(sales_list_url(request))
    # Clear out any run that died without saying so before deciding whether
    # one is genuinely in progress - otherwise a single killed thread locks
    # this page out permanently. Asked before the dates, as it always was;
    # asked again, with the job's creation, by start_sales_import.
    if active_import() is not None:
        messages.error(request, ALREADY_RUNNING)
        return redirect(sales_list_url(request))

    start = _parse_date(request.POST.get("start_date"))
    end = _parse_date(request.POST.get("end_date"))
    if not start or not end:
        messages.error(request, "Renseignez les deux dates.")
        return redirect(sales_list_url(request))
    if start > end:
        messages.error(request, "La date de début est après la date de fin.")
        return redirect(sales_list_url(request))

    # The one start of a sales import (recipes/importing.py): an automatic
    # import starting in between makes this one refused, never a second.
    if start_sales_import(start, end, trigger=SalesImportJob.Trigger.MANUAL) is None:
        messages.error(request, ALREADY_RUNNING)
    return redirect(sales_list_url(request))


def sales_import_status(request, job_id):
    # As invoices.views.gather_status: a dead run is reaped where it is polled.
    SalesImportJob.reap_stale()
    return render(
        request,
        "recipes/_sales_import_status.html",
        {"job": get_object_or_404(SalesImportJob, pk=job_id)},
    )


def cancel_sales_import(request, job_id):
    if request.method != "POST":
        return redirect(sales_list_url(request))
    job = get_object_or_404(SalesImportJob, pk=job_id)
    if job.is_active:
        job.cancel_requested = True
        job.save(update_fields=["cancel_requested"])
    return render(request, "recipes/_sales_import_status.html", {"job": job})


def _parse_date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def sales_list(request):
    """ "Recettes & ventes", on the sales: the till import, a sale typed by
    hand, the sale documents and every recorded sale.

    Manual entries are for what the till never saw - a tab settled off the
    books, a private event. They're stored under their own source, so an
    import can correct its own figures without touching them (see
    RecipeSale's uniqueness constraint).
    """
    if request.method == "POST":
        form = ManualSaleForm(request.POST)
        if form.is_valid():
            sale = form.save()
            messages.success(request, f"{sale.quantity} × {sale.recipe.name} le {sale.sold_on:%d/%m/%Y}.")
            return redirect(sales_list_url(request))
        return render_menu(request, "sales", form=form)
    return render_menu(request, "sales")


def sales_delete(request, pk):
    """Only hand-entered rows can be deleted here - an imported figure is a
    record of what the till reported, and correcting it means re-importing,
    not editing it away."""
    if request.method != "POST":
        return redirect(sales_list_url(request))
    sale = get_object_or_404(RecipeSale, pk=pk)
    if sale.source != MANUAL_SALE_SOURCE:
        messages.error(request, "Seules les ventes saisies à la main peuvent être supprimées ici.")
    else:
        sale.delete()
        messages.success(request, "Vente supprimée.")
    return redirect(sales_list_url(request))


def pos_products_bulk(request):
    """Set aside several till products at once.

    With a hundred-odd unmatched products after a first import, most of which
    are food, coffee or one-off oddities, doing this a row at a time means a
    hundred full page reloads. Only "ignore" is offered in bulk: linking needs
    a different recipe per product, so there's nothing to batch, and a bulk
    action that quietly linked the wrong ones would be exactly the silent
    mis-attribution the rest of this app works hard to avoid.
    """
    if request.method != "POST":
        return redirect("recipes:pos_product_list")

    names = request.POST.getlist("selected")
    if not names:
        messages.error(request, "Aucun produit sélectionné.")
        return redirect("recipes:pos_product_list")

    with transaction.atomic():
        updated = PosProduct.objects.filter(name__in=names, recipe__isnull=True).update(ignored=True)
        # The ticks are on rows still to link, but the page can be stale: a
        # product linked meanwhile goes through set_aside, which takes its
        # sales and its happy-hour name off the recipe - one UPDATE would not.
        for product in PosProduct.objects.filter(name__in=names, recipe__isnull=False).select_related("recipe"):
            set_aside(product, ignored=True)
            updated += 1
    messages.success(request, f"{updated} produit{'s' if updated > 1 else ''} ignoré{'s' if updated > 1 else ''}.")
    return redirect("recipes:pos_product_list")


# --- « Factures de vente » (recipes/sale_files.py) -------------------------------------------------------------------

#: Said on a sale document's page and on the tab's card.
DOCUMENT_GONE = "Cette facture de vente n'existe plus."
ALREADY_SAVED_CREATE = "Facture de vente déjà enregistrée : la voici."
ALREADY_SAVED_UPDATE = "Facture de vente déjà enregistrée."
LINE_ADDED = "Ligne ajoutée : rien n'est enregistré avant « Enregistrer »."
#: What static/js/sale_document.js asks before a form beside the document's
#: own leaves what was typed (`data-leaves-lines`).
LEAVE_WARNING = "Les modifications de la facture n'ont pas été enregistrées et seront perdues. Continuer ?"
READ_DONE = (
    "Facture électronique ({kind}) {number} du {day} ajoutée{customer}. Reliez ses lignes à vos recettes ou "
    "articles, puis enregistrez."
)
CHECKS_FAILED = "Ses propres totaux ne tombent pas juste : voyez « Contrôles de la facture »."
FILE_DELETED_TOO = "Son fichier est supprimé aussi."
NOT_COUNTED_LINES = (
    "Cette facture ne compte ni dans les marges ni dans le stock : relier ses lignes ne change aucun chiffre."
)
CREDIT_NOTE_LINES = "Un avoir ne remet rien en stock sauf si vous reliez la ligne : un retour de marchandise seulement."
FILE_IS_THE_INVOICE = "Le fichier est la facture : il ne se remplace pas. Pour une autre facture, supprimez celle-ci."
#: The lines of a typed document against the total it states.
REST_FREE = "{amount} € n'ont pas de ligne : ils comptent sans recette ni article."
REST_DISCOUNT = "{amount} € de remise sur les lignes : réparti sur elles dans les marges."

#: The sale documents this login made, by the one-time value of the page that
#: made each (`jeton`), with what was posted: that page posted again - a
#: double tap, a phone resending after a slow answer - opens the document it
#: made rather than making it twice (invoices.views.create_manual_invoice's
#: rule). In the session, the last few.
SALE_DOCUMENTS_MADE = "sale_documents_made"
SALE_DOCUMENTS_KEPT = 20
JETON = "jeton"
#: « + Ajouter une ligne » without JavaScript: a submit button of its own.
ADD_ROW = "ajouter_ligne"
#: Where an electronic invoice's ties saved come back to on its page: what
#: follows tying is its « Règlement ».
PAYMENTS = "reglements"
#: « Règlement »'s search of a credit to link (`entree`): a GET of the
#: page, ignored without « Banque ».
CREDIT_SEARCH = "entree"
#: Above « Règlement », for a reader without JavaScript: its forms leave the
#: page, and what was typed on the document with it (static/js/sale_document.js
#: hides it, and asks instead).
SAVE_BEFORE_PAYMENTS = "Enregistrez la facture avant de rattacher une entrée : ce qui n'est pas enregistré est perdu."
NO_CREDIT_PROPOSED = "Aucune entrée proposée à ces dates."
#: What the bank's automatic pass did after a read or a save (spec §3.7),
#: worded from who reads it: « Recettes & ventes » alone is told no bank
#: date, amount or payer (bank critique 7).
LINKED_TO_CREDIT = "Rattachée automatiquement à l'entrée du {day} ({amount} €)."
LINKED_WITHOUT_DETAIL = "Réglée par une entrée de « Banque », rattachée automatiquement."
BANK_PASS_FAILED = (
    "Le rapprochement bancaire n'a pas pu se faire : « Rapprocher automatiquement » sur Banque le refera."
)
#: The stored file's kind as its page shows it: a PDF framed, a photo drawn,
#: anything else to download.
FRAMED = (".pdf",)
DRAWN = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {plural if count > 1 else singular}"


def _tab_url(request, **extra) -> str:
    """The « Ventes » tab as the reader had it, on its « Factures de vente »
    card - and `extra` (the card's `date` given back)."""
    url = sales_list_url(request)
    extra = {name: value for name, value in extra.items() if value}
    if extra:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(extra)}"
    return f"{url}#{SALE_CARD}"


def _document_url(request, pk) -> str:
    """A sale document's page, the tab's parameters kept."""
    return f"{reverse('recipes:sale_document_update', args=[pk])}{kept_query(request)}"


def _made(request) -> tuple[str, str, int | None]:
    """(the page's `jeton`, what it posted, the document that page already
    made with that very post - None for a new submission)."""
    token = request.POST.get(JETON, "")[:64]
    digest = posted_digest(request)
    made_pk, made_from = request.session.get(SALE_DOCUMENTS_MADE, {}).get(token, (None, ""))
    if not token or made_from != digest or made_pk is None:
        return token, digest, None
    return token, digest, made_pk if SaleDocument.objects.filter(pk=made_pk).exists() else None


def _remember(request, token: str, digest: str, document: SaleDocument) -> None:
    if not token:
        return
    made = request.session.get(SALE_DOCUMENTS_MADE, {})
    made[token] = (document.pk, digest)
    request.session[SALE_DOCUMENTS_MADE] = dict(list(made.items())[-SALE_DOCUMENTS_KEPT:])


def sale_document_form(request, pk=None):
    """A « facture de vente »: a new one typed (`pk` None), or one saved -
    typed (its header, its file, a formset of lines) or an electronic
    invoice (its figures printed, its lines tied in a grid by pk).

    Each page is one submission (`jeton`): posted again as it was, it opens
    what it made. « + Ajouter une ligne » without JavaScript draws the page
    again with one more row, nothing saved, nothing refused. A document
    deleted meanwhile (another tab) is said, never a 404 on a POST."""
    if pk is None:
        document = SaleDocument()
    else:
        document = SaleDocument.objects.filter(pk=pk).first()
        if document is None:
            if request.method != "POST":
                raise Http404
            messages.info(request, DOCUMENT_GONE, extra_tags=SALE_CARD)
            return redirect(_tab_url(request))
    if request.method == "POST" and not document.is_einvoice and ADD_ROW in request.POST:
        return _add_a_row(request, document)
    token = digest = ""
    if request.method == "POST":
        token, digest, made = _made(request)
        if made is not None and (pk is None or made == document.pk):
            if pk is None:
                messages.info(request, ALREADY_SAVED_CREATE)
                return redirect(_document_url(request, made))
            messages.info(request, ALREADY_SAVED_UPDATE, extra_tags="" if document.is_einvoice else SALE_CARD)
            if document.is_einvoice:
                return redirect(f"{_document_url(request, document.pk)}#{PAYMENTS}")
            return redirect(_tab_url(request))
    if document.is_einvoice:
        return _einvoice_page(request, document, token, digest)
    return _typed_page(request, document, token, digest)


def _line_formset_class(document):
    """A new document opens with one row to type, a saved one with its lines
    only (CLAUDE.md « Formsets: no spare row on a saved record »)."""
    return SaleDocumentLineFormSet if document.pk else SaleDocumentLineFormSetNew


def _add_a_row(request, document):
    """« + Ajouter une ligne » without JavaScript: the page drawn again from
    what was posted, one row more, NOTHING validated - reading a form's
    errors validates it, and the half-typed rows would say « Quantité :
    tapez un nombre. » on a click that only asked for a row - and nothing
    saved. A browser never sends a file back: it is asked again."""
    data = request.POST.copy()
    total = data.get("lines-TOTAL_FORMS", "")
    count = int(total) if is_id(total) else 0
    data["lines-TOTAL_FORMS"] = str(min(count + 1, MAX_SALE_LINES))
    form = SaleDocumentForm(data, instance=document)
    formset = _line_formset_class(document)(data, instance=document)
    messages.info(request, LINE_ADDED)
    if request.FILES:
        messages.info(request, FILE_AGAIN)
    return _draw_typed(request, document, form, formset, hide_errors=True)


def _sells_something(form, formset) -> bool:
    """A total typed, or a line kept: a typed document holds one or the other."""
    if form.cleaned_data.get("stated_total_ttc") is not None:
        return True
    return any(line_form.cleaned_data and not line_form.cleaned_data.get("DELETE") for line_form in formset.forms)


def _typed_page(request, document, token: str, digest: str):
    """A typed document: its header and file (SaleDocumentForm), its lines
    (the formset). Saved, back to the tab's card; refused, the page again,
    everything typed kept - but the file, which a browser never sends back."""
    formset_class = _line_formset_class(document)
    if request.method != "POST":
        return _draw_typed(request, document, SaleDocumentForm(instance=document), formset_class(instance=document))
    form = SaleDocumentForm(request.POST, request.FILES, instance=document)
    formset = formset_class(request.POST, instance=document)
    outcome = None
    if form.is_valid() and formset.is_valid():
        if not _sells_something(form, formset):
            form.add_error(None, NOTHING_SOLD)
        else:
            outcome = _save_typed(form, formset)
    if outcome is GONE:
        messages.info(request, DOCUMENT_GONE, extra_tags=SALE_CARD)
        return redirect(_tab_url(request))
    if outcome is None:
        if request.FILES.get("source_file") and "source_file" not in form.errors:
            form.add_error("source_file", FILE_AGAIN)
        return _draw_typed(request, document, form, formset)
    _remember(request, token, digest, outcome.document)
    messages.success(request, f"{outcome.document} enregistrée.", extra_tags=SALE_CARD)
    if outcome.prices_written:
        messages.info(
            request,
            f"Prix de la carte écrit sur {_plural(outcome.prices_written, 'ligne', 'lignes')}.",
            extra_tags=SALE_CARD,
        )
    for line_form in formset.forms:
        if line_form.cleared and line_form.instance.pk:
            messages.info(request, CONSUMED_CLEARED.format(label=line_form.instance.shown_name), extra_tags=SALE_CARD)
    _say_bank_pass(request, outcome, SALE_CARD)
    return redirect(_tab_url(request))


#: `_save_typed`'s answer for a document deleted in another tab while this
#: save ran (sale_files.DocumentGone): said as on a document already gone.
GONE = object()


def _save_typed(form, formset):
    """The typed document saved with its file, or None and the refusal on
    the file's field: an electronic invoice, a file another document holds,
    a purchase's file (recipes/sale_files.py) - or GONE, the document
    deleted meanwhile."""
    upload = form.cleaned_data.get("source_file")
    try:
        if not upload:
            return save_typed(form, formset, remove_file=bool(form.cleaned_data.get("retirer_fichier")))
        with staged(upload) as path:
            refusal = typed_file_problem(path, upload.name, form.instance)
            if refusal:
                form.add_error("source_file", refusal)
                return None
            return save_typed(form, formset, staged_path=path, upload_name=upload.name)
    except SaleFileRefused as refused:
        form.add_error("source_file", str(refused))
        return None
    except DocumentGone:
        return GONE


def _einvoice_page(request, document, token: str, digest: str):
    """An electronic invoice: its date of sale, how it counts and a note
    typed (SaleEInvoiceHeaderForm); its lines tied (SaleTiesForm), proposed
    on a GET (recipes/sale_lines.py). Saved, back to its own page - its
    « Règlement » is what follows."""
    lines = list(document.lines.select_related("recipe", "stock_type").order_by("id"))
    articles = {line.stock_type_id for line in lines if line.stock_type_id}
    unit_costs = read_unit_costs(articles) if articles else {}
    if request.method == "POST":
        header = SaleEInvoiceHeaderForm(request.POST, instance=document)
        ties = SaleTiesForm(document, lines, data=request.POST, unit_costs=unit_costs)
        if header.is_valid() and ties.is_valid():
            with transaction.atomic():
                document = header.save()
                saved = ties.save()
            _remember(request, token, digest, document)
            messages.success(request, f"{document} enregistrée.")
            if saved.tied:
                messages.info(
                    request, f"{_plural(saved.tied, 'ligne reliée', 'lignes reliées')} à une recette ou un article."
                )
            for label in saved.cleared:
                messages.info(request, CONSUMED_CLEARED.format(label=label))
            return redirect(f"{_document_url(request, document.pk)}#{PAYMENTS}")
    else:
        header = SaleEInvoiceHeaderForm(instance=document)
        ties = SaleTiesForm(document, lines, proposals=proposals(document, lines), unit_costs=unit_costs)
    return _draw(
        request,
        document,
        lines,
        {
            "form": header,
            "ties": ties,
            "facts": _facts(document, lines, kept_query(request)),
            "checks": document.einvoice_checks,
            "failed_checks": sum(1 for check in document.einvoice_checks if not check.get("passed")),
            "lines_said": NOT_COUNTED_LINES
            if not document.counts
            else (CREDIT_NOTE_LINES if document.total_ttc_of(lines) < 0 else ""),
            "file_is_the_invoice": FILE_IS_THE_INVOICE,
            "totals": _totals(document, lines),
            "consumed_zero": CONSUMED_ZERO,
        },
    )


def _draw_typed(request, document, form, formset, *, hide_errors=False):
    lines = list(document.lines.select_related("recipe")) if document.pk else []
    return _draw(
        request,
        document,
        lines,
        {
            "form": form,
            "formset": formset,
            "hide_errors": hide_errors,
            "known_customers": known_customers(),
            "known_customers_list": KNOWN_CUSTOMERS_LIST,
            "totals": _totals(document, lines) if document.pk else None,
        },
    )


def _draw(request, document, lines, context: dict):
    """The page of `document` (new or saved), its `lines` read, with
    `context`: what both kinds draw - its title, its ways back, its file,
    its doubts, and once saved its « Règlement » with the messages said
    there."""
    saved = document.pk is not None
    if not saved:
        title = "Nouvelle facture de vente"
    elif document.reference:
        title = f"Facture de vente n° {document.reference}"
    else:
        title = f"Facture de vente du {document.sold_on:%d/%m/%Y}"
    here = (
        reverse("recipes:sale_document_update", args=[document.pk])
        if saved
        else reverse("recipes:sale_document_create")
    )
    stored = document.source_file.name if saved and document.source_file else ""
    extension = PurePosixPath(stored).suffix.lower()
    doubts = []
    if saved:
        doubts = counted_twice(document)
        no_final = deposit_doubts([document]).get(document.pk)
        if no_final:
            doubts.append(no_final)
    here_url = f"{here}{kept_query(request)}"
    top_messages, payment_messages = _messages_by_place(request, saved)
    return render(
        request,
        "recipes/sale_document_form.html",
        {
            "document": document,
            "saved": saved,
            # A page drawn in answer to a POST holds what was typed and not
            # saved (a successful save always redirects): its form says so,
            # and sale_document.js asks before any form leaves it.
            "unsaved": request.method == "POST",
            "page_title": title,
            "back_url": _tab_url(request),
            "here_url": here_url,
            JETON: secrets.token_urlsafe(16),
            "file_url": reverse("recipes:sale_document_file", args=[document.pk]) if stored else "",
            "file_name": PurePosixPath(stored).name,
            "file_framed": extension in FRAMED,
            "file_drawn": extension in DRAWN,
            "download_param": DOWNLOAD_PARAM,
            "delete_url": f"{reverse('recipes:sale_document_delete', args=[document.pk])}{kept_query(request)}"
            if saved
            else "",
            "delete_question": DELETE_QUESTION.format(label=document.label) if saved else "",
            "leave_warning": LEAVE_WARNING,
            "doubts": doubts,
            "kind_label": document.kind_label if saved else "",
            "payments": _payments(request, document, lines, here, here_url) if saved else None,
            "save_before_payments": SAVE_BEFORE_PAYMENTS,
            "no_credit_proposed": NO_CREDIT_PROPOSED,
            "top_messages": top_messages,
            "payment_messages": payment_messages,
            **context,
        },
    )


def _messages_by_place(request, saved: bool) -> tuple[list, list]:
    """(the page's messages said at the top, those said in « Règlement »)
    - the latter tagged PAYMENTS by Banque's action its forms post to
    (bank.views.SALE_MESSAGE_PLACES), said where their redirect lands
    (`#reglements`). Read once, which also marks them said."""
    top, payments = [], []
    for message in get_messages(request):
        tags = (message.extra_tags or "").split()
        (payments if saved and PAYMENTS in tags else top).append(message)
    return top, payments


def _payments(request, document, lines, here: str, here_url: str) -> dict:
    """« Règlement » (spec §5.5): what the bank has paid of `document` -
    the allocation's state, for whoever opens the page - and, with
    « Banque » (`access_of`: hiding a form is never the boundary, Banque's
    action is), the credits paying it with their shares, those that could
    (sale_reconcile.credits_for) and the credit search (`entree`, ignored
    without it). Every form posts to Banque's `bank_line_action`, coming
    back here (`#reglements`)."""
    allocation = read_links()
    payments = payment_context(document, allocation, access_of(request), lines)
    if not payments["sees_bank"]:
        return payments
    # here: bank reads this module
    from bank import sale_reconcile

    action = url_for_each("bank:bank_line_action")
    payments["links"] = [{"link": link, "action": action(link.fact.credit_pk)} for link in payments["links"]]
    # One form per option `credits_for` keeps (each holds this document),
    # each naming every invoice it posts with what it still asks: a sum's
    # reason names none, and posting the first option alone linked an
    # invoice the page never showed and left the other sums out of reach.
    payments["proposals"] = [
        {
            "offer": offer,
            "action": action(offer.line.pk),
            "options": [list(option) for option in offer.match.options],
        }
        for offer in sale_reconcile.credits_for(document, allocation)
    ]
    query = request.GET.get(CREDIT_SEARCH, "").strip()
    payments["query"] = query
    payments["search_action"] = f"{here}#{PAYMENTS}"
    payments["kept_fields"] = kept_fields(request)
    payments["back"] = f"{here_url}#{PAYMENTS}"
    if query:
        found, more = sale_reconcile.credits_found_for(document, query, allocation)
        payments["found"] = [{"found": one, "action": action(one.line.pk)} for one in found]
        payments["more_found"] = more
    return payments


def _totals(document, lines) -> dict:
    """What the page says of a document's money: its lines' total, the total
    it states, what its lines miss of it (spec §2.2), what was already paid
    and what is left to pay."""
    lines_ttc = SaleDocument.lines_ttc_of(lines)
    rest = ""
    if not document.is_einvoice and document.lines_differ_of(lines):
        gap = document.stated_total_ttc - lines_ttc
        sentence = REST_FREE if gap > 0 else REST_DISCOUNT
        rest = sentence.format(amount=format_money(abs(gap)))
    return {
        "lines": lines_ttc,
        "stated": document.stated_total_ttc,
        "rest": rest,
        "prepaid": document.prepaid_ttc,
        "to_pay": document.to_pay_of(lines),
    }


def _facts(document, lines, kept: str = "") -> list[dict]:
    """« Ce que dit la facture »: an electronic invoice's own figures,
    printed - never typed (spec §5.3). `kept` is the tab's parameters
    (`kept_query`), which the corrected invoice's address carries like every
    other address of the tab."""

    def euros(value) -> str:
        return f"{format_money(value)} €"

    facts = [
        {"label": "Numéro", "value": document.reference or "sans numéro"},
        {
            "label": "Date de la facture",
            "value": f"{document.einvoice_issued_on:%d/%m/%Y}" if document.einvoice_issued_on else "—",
        },
    ]
    if document.einvoice_delivered_on:
        facts.append({"label": "Livraison", "value": f"{document.einvoice_delivered_on:%d/%m/%Y}"})
    customer = document.customer or "—"
    if document.customer_identifier:
        customer = f"{customer} ({document.customer_identifier})"
    facts.append({"label": "Client", "value": customer})
    seller = document.seller_name or "—"
    if document.seller_siren:
        seller = f"{seller} (SIREN {siren_text(document.seller_siren)})"
    facts.append({"label": "Vendeur", "value": seller})
    facts.append({"label": "Type", "value": document.kind_label_of(document.total_ttc_of(lines))})
    if document.einvoice_preceding_number:
        corrected = (
            SaleDocument.objects.filter(reference__iexact=document.einvoice_preceding_number)
            .exclude(pk=document.pk)
            .order_by("-sold_on", "-pk")
            .first()
        )
        facts.append(
            {
                "label": "Facture corrigée",
                "value": document.einvoice_preceding_number,
                "url": f"{reverse('recipes:sale_document_update', args=[corrected.pk])}{kept}" if corrected else "",
            }
        )
    if document.stated_total_ht is not None:
        facts.append({"label": "Total HT", "value": euros(document.stated_total_ht)})
        if document.stated_total_ttc is not None:
            facts.append({"label": "TVA", "value": euros(document.stated_total_ttc - document.stated_total_ht)})
    if document.stated_total_ttc is not None:
        facts.append({"label": "Total TTC", "value": euros(document.stated_total_ttc)})
    if document.prepaid_ttc is not None:
        facts.append({"label": "Déjà réglé", "value": euros(document.prepaid_ttc)})
    if document.payable_ttc is not None:
        facts.append({"label": "Reste à payer", "value": euros(document.payable_ttc)})
    if document.adjustment_ht:
        rate = (
            f" (TVA {plain_number(document.adjustment_vat_rate * 100)} %)"
            if document.adjustment_vat_rate is not None
            else ""
        )
        facts.append({"label": "Frais et remises", "value": f"{euros(document.adjustment_ht)} HT{rate}"})
    return facts


def sale_document_read(request):
    """« Lire la facture », the « Ventes » tab's card: the bar's electronic
    invoice read and created at once, with its lines, its file and how it
    counts as chosen on the card (recipes/sale_files.read_einvoice_upload) -
    then its page, where its lines are tied. Refused, back to the card with
    the reason, as it is: einvoice's own sentences included (a UTF-16 file, a
    DOCTYPE, no EN 16931 invoice), anything else a fixed sentence and the
    detail to the log - never a library's words (CLAUDE.md « What a page may
    say about an error »). A date the person typed that was refused comes
    back in the card's box, and the « Compte » chosen with it."""
    if request.method != "POST":
        return redirect(_tab_url(request))
    token, digest, made = _made(request)
    if made is not None:
        messages.info(request, ALREADY_SAVED_CREATE)
        return redirect(_document_url(request, made))
    try:
        outcome = read_einvoice_upload(
            request.FILES.get("fichier"),
            typed_date_text=request.POST.get("date", ""),
            counting=request.POST.get("compte", ""),
        )
    except Exception as exc:  # noqa: BLE001 - said in the card, never a 500 (the detail is logged)
        said = error_for_page(
            exc, said=(EInvoiceError, SaleFileRefused), log=logger, what="Lecture d'une facture de vente électronique"
        )
        messages.error(request, said, extra_tags=SALE_CARD)
        typed = getattr(exc, "typed_date", None)
        # « Compte » given back with the date (the default needs no word):
        # drawn again on its default, a « Déjà comptée par la caisse »
        # refused was counted by the next file sent without a second look.
        counting = request.POST.get("compte", "")
        if counting not in SaleDocument.Counting.values or counting == SaleDocument.Counting.COUNTED:
            counting = ""
        return redirect(_tab_url(request, date=typed.isoformat() if typed else "", compte=counting))
    _remember(request, token, digest, outcome.document)
    _say_read(request, outcome)
    return redirect(_document_url(request, outcome.document.pk))


def _say_read(request, outcome) -> None:
    """What « Lire la facture » made, said on the document's page (spec §3.7)."""
    document = outcome.document
    messages.success(
        request,
        READ_DONE.format(
            kind=document.einvoice_format,
            number=f"n° {document.reference}" if document.reference else "sans numéro",
            day=f"{document.sold_on:%d/%m/%Y}",
            customer=f" (client « {document.customer} »)" if document.customer else "",
        ),
    )
    if outcome.date_said:
        messages.info(request, outcome.date_said)
    for said in outcome.said:
        messages.add_message(request, messages.WARNING if said.level == "warning" else messages.INFO, said.text)
    if outcome.proposals:
        messages.info(
            request,
            f"{_plural(outcome.proposals, 'ligne proposée', 'lignes proposées')} : vérifiez-"
            f"{'les' if outcome.proposals > 1 else 'la'} avant d'enregistrer.",
        )
    if outcome.failed_checks:
        messages.warning(request, CHECKS_FAILED)
    _say_bank_pass(request, outcome)


def _say_bank_pass(request, outcome, tags: str = "") -> None:
    """What the bank's automatic pass did once a document was read or saved
    (`linked`, `bank_failed`, spec §3.7), worded from who reads it: with
    « Banque » the credit's day and amount; without, that a credit of
    « Banque » pays it - no date, no amount, no payer. A failure is said:
    the document is saved, Banque's « Rapprocher automatiquement » runs it
    again."""
    if outcome.linked:
        if access_of(request).allows("bank"):
            for line in outcome.linked:
                messages.success(
                    request,
                    LINKED_TO_CREDIT.format(day=f"{line.operation_date:%d/%m/%Y}", amount=format_money(line.amount)),
                    extra_tags=tags,
                )
        else:
            messages.success(request, LINKED_WITHOUT_DETAIL, extra_tags=tags)
    if outcome.bank_failed:
        messages.warning(request, BANK_PASS_FAILED, extra_tags=tags)


@require_safe
@xframe_options_sameorigin
def sale_document_file(request, pk):
    """A sale document's own file, under its download name
    (invoices/filenames.py::sale_download_name): a PDF or a photo inline -
    the document's page frames it -, anything else a sandboxed download
    (accounts.views.file_response), `?telecharger=1` to save. Pages link
    here, never to `source_file.url`. Only a file under `ventes/`: a row
    naming another folder (an older archive, a hand edit) would give a
    purchase's PDF to an employee given « Recettes & ventes » through this
    route."""
    document = get_object_or_404(SaleDocument.objects.prefetch_related("lines__recipe"), pk=pk)
    name = document.source_file.name if document.source_file else ""
    # Judged on the name resolved, as /fichiers/ judges it
    # (accounts.access.areas_of_file): « ventes/../invoices/… » stays inside
    # the media folder, so open_stored alone would serve a purchase's file.
    if not posixpath.normpath(name.replace("\\", "/")).startswith(SALE_FILES_FOLDER):
        raise Http404
    handle = open_stored(name)
    if handle is None:
        raise Http404
    return file_response(handle, sale_download_name(document), download=request.GET.get(DOWNLOAD_PARAM) == "1")


def sale_document_delete(request, pk):
    """A sale document deleted - its lines, its file once that commits, its
    bank links (the credits stay on Banque): recipes/sale_files.py. A
    document gone already (a double tap, another tab) is said, never a 404
    (CLAUDE.md « … n'existe plus. »)."""
    if request.method != "POST":
        return redirect(_tab_url(request))
    document = SaleDocument.objects.filter(pk=pk).first()
    deleted = delete_document(document) if document is not None else None
    if deleted is None:
        messages.info(request, DOCUMENT_GONE, extra_tags=SALE_CARD)
        return redirect(_tab_url(request))
    said = [f"{deleted.label} supprimée."]
    if deleted.had_file:
        said.append(FILE_DELETED_TOO)
    if deleted.links:
        detached = _plural(deleted.links, "règlement bancaire détaché", "règlements bancaires détachés")
        said.append(f"{detached} : les entrées restent sur Banque.")
    messages.success(request, " ".join(said), extra_tags=SALE_CARD)
    return redirect(_tab_url(request))
