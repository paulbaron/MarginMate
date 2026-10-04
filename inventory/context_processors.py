from accounts.access import access_of
from accounts.tenancy import current_tenant
from config.navigation import counted_when_drawn

from .models import Product


def review_count(request):
    """What the "Produits & charges" badge says: the same queue the page lists
    (inventory.views.review_panel_context). A charge item has no stock
    item and never will, so counting it here put 110 over a page of 97.
    Nothing when no tenant is bound (multi mode's login, 404 and CSRF pages):
    there is no queue to count, and no database to count it in - nor for
    an employee the link is not drawn for (accounts/access.py). Counted
    when base.html draws it (config.navigation.counted_when_drawn)."""
    if current_tenant() is None or not access_of(request).allows("products"):
        return {}
    return {
        "review_count_nav": counted_when_drawn(Product.objects.filter(stock_type__isnull=True, is_expense=False).count)
    }
