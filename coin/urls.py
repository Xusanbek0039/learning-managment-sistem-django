from django.urls import path
from . import views

app_name = 'coin'

urlpatterns = [
    # Market (User)
    path('market/', views.market_list, name='market_list'),
    path('market/<int:pk>/', views.market_detail, name='market_detail'),
    path('market/<int:pk>/like/', views.market_like, name='market_like'),
    path('market/<int:pk>/purchase/', views.market_purchase, name='market_purchase'),
    
    # Admin: Products
    path('dashboard/products/', views.admin_products, name='admin_products'),
    path('dashboard/products/add/', views.admin_product_add, name='admin_product_add'),
    path('dashboard/products/<int:pk>/edit/', views.admin_product_edit, name='admin_product_edit'),
    path('dashboard/products/<int:pk>/delete/', views.admin_product_delete, name='admin_product_delete'),
    path('dashboard/purchases/', views.admin_purchases, name='admin_purchases'),
    path('dashboard/purchases/<int:pk>/delivered/', views.admin_mark_delivered, name='admin_mark_delivered'),
    
    # Admin: Activities
    path('dashboard/activities/', views.admin_activities, name='admin_activities'),
]
