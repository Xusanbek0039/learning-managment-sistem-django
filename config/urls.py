
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("ai/", include("ai_yordamchi.urls")),
    path('admin/', admin.site.urls),
    path('', include('accounts.urls')),
    path('', include('coin.urls')),
    path('blog/', include('blog.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
