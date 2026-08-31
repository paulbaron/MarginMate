import threading
from collections import Counter
from decimal import Decimal, InvalidOperation
from itertools import groupby

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, UpdateView

from .ai_suggestions import _check_ollama_ready
from .forms import StockTypeForm
from .models import Product, StockMovement, StockType, SuggestionJob, UnitChoices
from .services import (
    link_product_to_stock_type,
    product_base_amount,
    refresh_invoice_statuses_for_product,
    unlink_product,
    update_product_conversion,
)
from .tasks import run_suggestion_job


def existing_categories():
    return StockType.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")


class StockListView(ListView):
    model = StockType
    template_name = "inventory/stock_list.html"
    context_object_name = "stock_types"

    def get_queryset(self):
        return StockType.objects.all().order_by("category", "name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rows = []
        for st in context["stock_types"]:
            movements_qs = (
                StockMovement.objects.filter(stock_type=st)
                .select_related("invoice_line__invoice__supplier", "invoice_line__product")
                .order_by("-invoice_line__invoice__invoice_date", "-created_at")
            )
            movements = []
            for m in movements_qs:
                line = m.invoice_line
                movements.append(
                    {
                        "movement": m,
                        "line": line,
                        "total_ttc": line.total_ht * (1 + line.vat_rate) if line else None,
                        "vat_percent": line.vat_rate * 100 if line else None,
                        # Same fallback the actual stock computation uses
                        # (total_volume when measured, else the item count) -
                        # showing total_volume unconditionally here made
                        # "Quantité achetée" read as 0 whenever a line had no
                        # measured volume, even though the real stock
                        # contribution was correctly computed from quantity.
                        "purchased_quantity": product_base_amount(line) if line else None,
                        # Clean display value for the inline edit input - a
                        # raw Decimal shows as "0.7000", not "0.7".
                        "stock_equivalent_display": (
                            f"{float(line.product.stock_equivalent):g}" if line else None
                        ),
                    }
                )
            rows.append(
                {
                    "stock_type": st,
                    "quantity": st.current_quantity,
                    "value_ht": st.current_value_ht,
                    "value_ttc": st.current_value_ttc,
                    "movements": movements,
                }
            )
        categories = [
            {"name": category or "Sans catégorie", "rows": list(group)}
            for category, group in groupby(rows, key=lambda row: row["stock_type"].category)
        ]
        context["categories"] = categories
        context["total_value_ht"] = sum((row["value_ht"] for row in rows), start=0)
        context["total_value_ttc"] = sum((row["value_ttc"] for row in rows), start=0)
        context["review_count"] = Product.objects.filter(stock_type__isnull=True).count()
        context["empty_stock_type_count"] = StockType.objects.filter(products__isnull=True).distinct().count()
        context["unit_choices"] = UnitChoices.choices
        return context


class CategoryAutocompleteMixin:
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["existing_categories"] = existing_categories()
        return context


class StockTypeCreateView(CategoryAutocompleteMixin, CreateView):
    model = StockType
    form_class = StockTypeForm
    template_name = "inventory/stock_type_form.html"
    success_url = reverse_lazy("inventory:stock_list")


class StockTypeUpdateView(CategoryAutocompleteMixin, UpdateView):
    model = StockType
    form_class = StockTypeForm
    template_name = "inventory/stock_type_form.html"
    success_url = reverse_lazy("inventory:stock_list")


def delete_stock_type(request, pk):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    stock_type = get_object_or_404(StockType, pk=pk)
    name = stock_type.name
    affected_products = list(stock_type.products.all())
    # Product.stock_type is SET_NULL and StockMovement.stock_type is CASCADE,
    # so this alone sends every associated product back to the review queue
    # and drops the stock type's ledger entries. That cascade happens at the
    # DB level, bypassing unlink_product() - so invoice statuses need
    # refreshing separately here, or their invoices would stay marked
    # COMPLETE despite now containing an unreviewed product again.
    stock_type.delete()
    for product in affected_products:
        refresh_invoice_statuses_for_product(product)
    messages.success(request, f'Type de stock "{name}" supprimé. Les produits associés sont repassés en vérification.')
    return redirect("inventory:stock_list")


def clear_empty_stock_types(request):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    empty = StockType.objects.filter(products__isnull=True).distinct()
    count = empty.count()
    empty.delete()
    if count:
        messages.success(request, f"{count} type(s) de stock vide(s) supprimé(s).")
    else:
        messages.info(request, "Aucun type de stock vide à supprimer.")
    return redirect("inventory:stock_list")


class ReviewQueueView(ListView):
    model = Product
    template_name = "inventory/review_queue.html"
    context_object_name = "products"

    def get_queryset(self):
        return (
            Product.objects.filter(stock_type__isnull=True)
            .select_related("supplier")
            .prefetch_related("invoice_lines")
            .order_by("raw_name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stock_types"] = StockType.objects.all()
        context["unit_choices"] = UnitChoices.choices
        context["existing_categories"] = existing_categories()
        context["latest_suggestion_job"] = SuggestionJob.objects.first()

        suggested = [p for p in context["products"] if p.ai_suggestion]
        confidence_counts = Counter(p.ai_suggestion.get("confidence", "?") for p in suggested)
        context["suggested_count"] = len(suggested)
        context["confidence_counts"] = confidence_counts
        return context


def remove_product(request, product_id):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    product = get_object_or_404(Product, pk=product_id)
    unlink_product(product)
    messages.success(request, f'"{product.raw_name}" retiré du stock et repassé en vérification.')
    return redirect("inventory:stock_list")


def edit_product_conversion(request, product_id):
    if request.method != "POST":
        return redirect("inventory:stock_list")
    product = get_object_or_404(Product, pk=product_id)
    product_unit = request.POST.get("product_unit")
    stock_equivalent = _parse_positive_decimal(request.POST.get("stock_equivalent", ""), default=None)
    if product_unit not in UnitChoices.values or stock_equivalent is None:
        messages.error(request, "Unité ou facteur invalide.")
        return redirect("inventory:stock_list")
    update_product_conversion(product, unit=product_unit, stock_equivalent=stock_equivalent)
    messages.success(request, f'"{product.raw_name}" mis à jour (facteur {stock_equivalent}, {product.stock_type}).')
    return redirect("inventory:stock_list")


def trigger_suggest_products(request):
    if request.method != "POST":
        return redirect("inventory:review_queue")

    active_job = SuggestionJob.objects.filter(
        status__in=[SuggestionJob.Status.PENDING, SuggestionJob.Status.RUNNING]
    ).first()
    if active_job:
        return redirect("inventory:review_queue")

    try:
        _check_ollama_ready()
    except RuntimeError as exc:
        messages.error(request, str(exc))
        return redirect("inventory:review_queue")

    job = SuggestionJob.objects.create()
    thread = threading.Thread(target=run_suggestion_job, args=(job.id,), daemon=True)
    thread.start()
    return redirect("inventory:review_queue")


def suggestion_job_status(request, job_id):
    job = get_object_or_404(SuggestionJob, pk=job_id)
    # Only reload the page from a polling request that just observed the job
    # finish - not on a fresh page load where the latest job already happens
    # to be SUCCESS, or every visit to the review queue would reload forever.
    is_poll = request.headers.get("HX-Request") == "true"
    return render(request, "inventory/_suggestion_job_status.html", {"job": job, "is_poll": is_poll})


def cancel_suggestion_job(request, job_id):
    if request.method != "POST":
        return redirect("inventory:review_queue")
    job = get_object_or_404(SuggestionJob, pk=job_id)
    if job.status in (SuggestionJob.Status.PENDING, SuggestionJob.Status.RUNNING):
        job.cancel_requested = True
        job.save(update_fields=["cancel_requested"])
        messages.info(request, "Annulation demandée - le modèle va s'arrêter dans quelques secondes.")
    return redirect("inventory:review_queue")


def _resolve_suggestion_stock_type(suggestion: dict) -> StockType | None:
    matched_id = suggestion.get("matched_stock_type_id")
    if matched_id:
        stock_type = StockType.objects.filter(pk=matched_id).first()
        if stock_type:
            return stock_type
    name = (suggestion.get("stock_type_name") or "").strip()
    if not name:
        return None
    unit = suggestion.get("new_stock_type_unit") or UnitChoices.UNIT
    category = (suggestion.get("new_stock_type_category") or "").strip()
    stock_type, _created = StockType.objects.get_or_create(
        name__iexact=name,
        defaults={"name": name, "unit": unit, "category": category},
    )
    return stock_type


def approve_all_suggestions(request):
    if request.method != "POST":
        return redirect("inventory:review_queue")

    products = Product.objects.filter(stock_type__isnull=True, ai_suggestion__isnull=False)
    approved = 0
    skip_reasons = Counter()
    for product in products:
        suggestion = product.ai_suggestion
        product_unit = suggestion.get("product_unit")
        stock_equivalent = _parse_positive_decimal(str(suggestion.get("stock_equivalent", "")), default=None)
        stock_type = _resolve_suggestion_stock_type(suggestion)

        reason = None
        if stock_type is None:
            reason = "aucun type de stock identifié"
        elif stock_equivalent is None:
            reason = f"facteur de conversion invalide ({suggestion.get('stock_equivalent')!r})"
        elif product_unit not in UnitChoices.values:
            reason = f"unité de produit invalide ({product_unit!r})"

        if reason:
            skip_reasons[reason] += 1
            # Clear it so "Suggérer avec l'IA" picks this product up again
            # instead of it being permanently stuck with a bad suggestion -
            # re-running costs nothing since only products without a
            # suggestion get sent to the model.
            product.ai_suggestion = None
            product.save(update_fields=["ai_suggestion"])
            continue

        link_product_to_stock_type(product, stock_type, unit=product_unit, stock_equivalent=stock_equivalent)
        approved += 1

    skipped = sum(skip_reasons.values())
    if skipped:
        detail = ", ".join(f"{count} ({reason})" for reason, count in skip_reasons.most_common())
        messages.warning(
            request,
            f"{approved} produit(s) rattaché(s) d'après l'IA. {skipped} ignoré(s) : {detail}. "
            "Ces produits sont repassés sans suggestion - relancez « Suggérer avec l'IA » pour leur en "
            "générer une nouvelle.",
        )
    elif approved:
        messages.success(request, f"{approved} produit(s) rattaché(s) automatiquement d'après les suggestions IA.")
    else:
        messages.info(request, "Aucune suggestion IA à approuver pour le moment.")
    return redirect("inventory:review_queue")


def _parse_positive_decimal(raw: str, default: Decimal) -> Decimal | None:
    """Returns the parsed value, `default` if blank, or None if invalid."""
    raw = raw.strip()
    if not raw:
        return default
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def assign_product(request, product_id):
    if request.method != "POST":
        return redirect("inventory:review_queue")

    product = get_object_or_404(Product, pk=product_id)
    existing_id = request.POST.get("existing_stock_type")
    new_name = request.POST.get("new_stock_type_name", "").strip()
    product_unit = request.POST.get("product_unit") or UnitChoices.UNIT
    stock_equivalent = _parse_positive_decimal(request.POST.get("stock_equivalent", ""), default=Decimal("1"))

    if stock_equivalent is None:
        messages.error(request, "L'équivalence en stock doit être un nombre positif.")
        return redirect("inventory:review_queue")

    if existing_id:
        stock_type = get_object_or_404(StockType, pk=existing_id)
    elif new_name:
        unit = request.POST.get("new_stock_type_unit") or UnitChoices.UNIT
        category = request.POST.get("new_stock_type_category", "").strip()
        stock_type, _ = StockType.objects.get_or_create(
            name__iexact=new_name,
            defaults={"name": new_name, "unit": unit, "category": category},
        )
    else:
        messages.error(request, "Choisissez un type existant ou donnez un nom pour en créer un nouveau.")
        return redirect("inventory:review_queue")

    link_product_to_stock_type(product, stock_type, unit=product_unit, stock_equivalent=stock_equivalent)
    messages.success(request, f'"{product.raw_name}" lié à "{stock_type.name}".')
    return redirect("inventory:review_queue")
