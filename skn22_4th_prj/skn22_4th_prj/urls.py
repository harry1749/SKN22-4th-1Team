from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("chat.urls")),
    path("auth/", include(("users.urls", "users_auth"), namespace="users_auth")),
    path("user/", include(("users.urls", "users"), namespace="users")),
    path("drug/", include(("drug.urls", "drug"), namespace="drug")),
    path("drugs/", include(("drug.urls", "drugs_compat"), namespace="drugs_compat")),  # Add compatibility for /drugs/
    path("api/drugs/", include(("drug.urls", "drugs_api"), namespace="drugs_api")),  # Compatibility with old drug link
]
