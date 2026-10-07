from django.db import transaction
from django.test import RequestFactory

from shop.models import OrderLog, Product
from shop.views import order_view


def test_order_view_logs_order_and_decrements_stock():
    with transaction.atomic():
        product = Product.objects.create(name="spec", stock=5)
        response = order_view(RequestFactory().get("/", {"quantity": "2"}), product.pk)
        product.refresh_from_db()
        assert response.status_code == 200
        assert product.stock == 3
        assert OrderLog.objects.filter(order__product=product).count() == 1
        transaction.set_rollback(True)
