from django.db.models import F

from .models import Order, Product


class OutOfStock(Exception):
    pass


def place_order(product_id, quantity):
    product = Product.objects.get(pk=product_id)
    Product.objects.filter(pk=product_id).update(stock=F("stock") - quantity)
    return Order.objects.create(product=product, quantity=quantity)
