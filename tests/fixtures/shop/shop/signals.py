from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Order, OrderLog


@receiver(post_save, sender=Order)
def log_order(sender, instance, created, **kwargs):
    if created:
        OrderLog.objects.create(order=instance, message=f"ordered {instance.quantity}")
