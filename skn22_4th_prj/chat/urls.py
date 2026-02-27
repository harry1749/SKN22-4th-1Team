from django.urls import path
from . import views

app_name = "chat"

urlpatterns = [
    path("", views.home, name="home"),
    path("smart-search/", views.smart_search, name="smart_search"),
    path("api/pharmacies/", views.pharmacy_api, name="pharmacy_api"),
]
