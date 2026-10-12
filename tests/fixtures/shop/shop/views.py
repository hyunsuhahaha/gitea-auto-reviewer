from django.http import JsonResponse

from .services import place_order


def order_view(request, product_id):
    order = place_order(product_id, int(request.GET.get("quantity", "1")))
    return JsonResponse({"order": order.pk})
