from django.urls import path
from .views import DrugSearchView, UsRoadmapView

app_name = "drug"

urlpatterns = [
    path("search/", DrugSearchView.as_view(), name="search"),
    path("us-roadmap/", UsRoadmapView.as_view(), name="us_roadmap"),
]
