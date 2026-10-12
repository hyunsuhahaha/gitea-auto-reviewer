from django.db import models


class Product(models.Model):
    name = models.CharField(max_length=100)
    stock = models.IntegerField()


class Order(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="orders")
    quantity = models.IntegerField()
    status = models.CharField(max_length=20, default="placed")
    created_at = models.DateTimeField(auto_now_add=True)


class OrderLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="logs")
    message = models.CharField(max_length=200)
