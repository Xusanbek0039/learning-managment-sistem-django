from django.urls import path
from . import views

app_name = 'ai_yordamchi'

urlpatterns = [
    path('', views.chat_home, name='chat_home'),
    path('new/', views.new_session, name='new_session'),
    path('session/<int:pk>/', views.chat_session, name='chat_session'),
    path('session/<int:pk>/send/', views.send_message, name='send_message'),
    path('session/<int:pk>/clear/', views.clear_history, name='clear_history'),
    path('session/<int:pk>/delete/', views.delete_session, name='delete_session'),
    
    # Admin
    path('admin/chats/', views.admin_chats, name='admin_chats'),
    path('admin/chats/<int:pk>/', views.admin_chat_detail, name='admin_chat_detail'),
]
