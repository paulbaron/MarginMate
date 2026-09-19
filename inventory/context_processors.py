from .models import Product


def review_count(request):
    """What the "Produits & charges" badge says: the same queue the page lists
    (inventory.views.review_panel_context). A poste of charge has no stock
    item and never will, so counting it here put 110 over a page of 97."""
    return {
        "review_count_nav": Product.objects.filter(stock_type__isnull=True, is_expense=False).count()
    }
