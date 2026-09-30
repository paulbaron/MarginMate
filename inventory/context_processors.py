from accounts.tenancy import current_tenant

from .models import Product


def review_count(request):
    """What the "Produits & charges" badge says: the same queue the page lists
    (inventory.views.review_panel_context). A poste of charge has no stock
    item and never will, so counting it here put 110 over a page of 97.
    Nothing when no espace is bound (multi mode's login, 404 and CSRF pages):
    there is no queue to count, and no database to count it in."""
    if current_tenant() is None:
        return {}
    return {
        "review_count_nav": Product.objects.filter(stock_type__isnull=True, is_expense=False).count()
    }
