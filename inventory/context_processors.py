from .models import Product


def review_count(request):
    return {"review_count_nav": Product.objects.filter(stock_type__isnull=True).count()}
