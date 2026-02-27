from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("chat.urls")),
    path("auth/", include("users.urls")),
    path("user/", include("users.urls")),
    path("drug/", include("drug.urls")),
    path("drugs/", include("drug.urls")),  # Add compatibility for /drugs/
    path("api/drugs/", include("drug.urls")),  # Compatibility with old drug link
]
