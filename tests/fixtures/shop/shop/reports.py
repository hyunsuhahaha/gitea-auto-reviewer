from .models import Order, Product


def placed_quantities():
    return list(Order.objects.filter(status="placed").values_list("quantity", flat=True))


def low_stock_products():
    return list(Product.objects.filter(stock__lt=5).values_list("name", flat=True))
