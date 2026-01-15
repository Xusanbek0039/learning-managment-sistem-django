from django.urls import path
from . import views

urlpatterns = [
    path('', views.home_view, name='home'),
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('professions/', views.professions_view, name='professions'),
    
    # Admin Panel URLs
    path('dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('dashboard/professions/', views.admin_professions, name='admin_professions'),
    path('dashboard/professions/add/', views.admin_profession_add, name='admin_profession_add'),
    path('dashboard/professions/<int:pk>/edit/', views.admin_profession_edit, name='admin_profession_edit'),
    path('dashboard/professions/<int:pk>/delete/', views.admin_profession_delete, name='admin_profession_delete'),
    path('dashboard/users/', views.admin_users, name='admin_users'),
]
