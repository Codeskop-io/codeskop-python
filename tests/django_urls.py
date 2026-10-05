from django.http import HttpResponse, JsonResponse
from django.urls import path


def order(request, pk):
    return JsonResponse({"id": pk})


def boom(request):
    raise ValueError("view exploded")


def health(request):
    return HttpResponse("ok")


urlpatterns = [
    path("orders/<int:pk>/", order),
    path("boom/", boom),
    path("healthz", health),
]
