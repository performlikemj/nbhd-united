from pathlib import Path

from django.http import HttpResponse
from django.urls import path

from config.urls import urlpatterns as product_urls


def signup(request):
    return HttpResponse(Path(__file__).with_name("signup.html").read_text())


urlpatterns = [path("local-test/signup/", signup), *product_urls]
