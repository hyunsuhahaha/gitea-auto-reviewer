from django.urls import path

from .views import order_view

urlpatterns = [path("products/<int:product_id>/order/", order_view)]
