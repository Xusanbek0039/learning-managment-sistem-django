from django.urls import path
from . import views

urlpatterns = [
    path('', views.home_view, name='home'),
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    
    # Professions & Courses
    path('professions/', views.professions_view, name='professions'),
    path('professions/<int:pk>/', views.profession_detail, name='profession_detail'),
    path('professions/<int:pk>/enroll/', views.enroll_course, name='enroll_course'),
    path('professions/<int:pk>/manage/', views.manage_lessons, name='manage_lessons'),
    path('professions/<int:pk>/add-lesson/', views.add_lesson, name='add_lesson'),
    path('professions/<int:pk>/add-section/', views.add_section, name='add_section'),
    path('section/<int:pk>/edit/', views.edit_section, name='edit_section'),
    path('section/<int:pk>/delete/', views.delete_section, name='delete_section'),
    
    # Lessons
    path('lessons/<int:pk>/', views.lesson_view, name='lesson_view'),
    path('lessons/<int:pk>/edit/', views.edit_lesson, name='edit_lesson'),
    path('lessons/<int:pk>/delete/', views.delete_lesson, name='delete_lesson'),
    path('video/<int:pk>/watched/', views.mark_video_watched, name='mark_video_watched'),
    path('homework/<int:pk>/submit/', views.submit_homework, name='submit_homework'),
    
    # Tests
    path('test/<int:pk>/start/', views.start_test, name='start_test'),
    path('test/<int:pk>/submit/', views.submit_test, name='submit_test'),
    path('test/result/<int:pk>/', views.test_result, name='test_result'),
    path('test/<int:pk>/questions/', views.manage_test_questions, name='manage_test_questions'),
    path('test/<int:pk>/questions/add/', views.add_test_question, name='add_test_question'),
    path('question/<int:pk>/delete/', views.delete_test_question, name='delete_test_question'),
    
    # Profile
    path('profile/', views.profile_view, name='profile'),
    path('profile/edit/', views.profile_edit_view, name='profile_edit'),
    path('profile/change-password/', views.change_password_view, name='change_password'),
    path('profile/statistics/', views.student_statistics, name='my_statistics'),
    path('profile/statistics/pdf/', views.export_student_pdf, name='my_statistics_pdf'),
    
    # Leaderboard
    path('leaderboard/', views.leaderboard, name='leaderboard'),
    
    # Teacher panel
    path('homework-submissions/', views.homework_submissions, name='homework_submissions'),
    path('homework/<int:pk>/grade/', views.grade_homework, name='grade_homework'),
    path('test-results/', views.all_test_results, name='all_test_results'),
    
    # Messages
    path('messages/', views.messages_view, name='messages'),
    path('messages/<int:pk>/', views.message_detail, name='message_detail'),
    path('messages/<int:pk>/read/', views.mark_message_read, name='mark_message_read'),
    
    # Admin Panel
    path('dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('dashboard/professions/', views.admin_professions, name='admin_professions'),
    path('dashboard/professions/add/', views.admin_profession_add, name='admin_profession_add'),
    path('dashboard/professions/<int:pk>/edit/', views.admin_profession_edit, name='admin_profession_edit'),
    path('dashboard/professions/<int:pk>/delete/', views.admin_profession_delete, name='admin_profession_delete'),
    path('dashboard/users/', views.admin_users, name='admin_users'),
    path('dashboard/users/<int:pk>/', views.admin_user_view, name='admin_user_view'),
    path('dashboard/users/<int:pk>/edit/', views.admin_user_edit, name='admin_user_edit'),
    path('dashboard/users/<int:pk>/block/', views.admin_user_block, name='admin_user_block'),
    path('dashboard/users/<int:pk>/delete/', views.admin_user_delete, name='admin_user_delete'),
    path('dashboard/users/<int:pk>/statistics/', views.student_statistics, name='user_statistics'),
    path('dashboard/users/<int:pk>/statistics/pdf/', views.export_student_pdf, name='user_statistics_pdf'),
    path('dashboard/users/<int:pk>/certificate/', views.issue_certificate, name='issue_certificate'),
    path('dashboard/statistics/', views.admin_statistics, name='admin_statistics'),
    path('dashboard/export-pdf/', views.export_system_pdf, name='export_system_pdf'),
    path('dashboard/messages/', views.admin_messages, name='admin_messages'),
    path('dashboard/messages/send/', views.admin_send_message, name='admin_send_message'),
    path('dashboard/payments/', views.admin_payments, name='admin_payments'),
    path('dashboard/payments/<int:pk>/paid/', views.admin_mark_paid, name='admin_mark_paid'),
    path('dashboard/payments/<int:pk>/remind/', views.send_payment_reminder, name='send_payment_reminder'),
    path('dashboard/payments/remind-all/', views.send_bulk_payment_reminders, name='send_bulk_payment_reminders'),
    
    # Admin: Sections
    path('dashboard/sections/', views.admin_sections, name='admin_sections'),
    path('dashboard/sections/add/', views.admin_section_add, name='admin_section_add'),
    path('dashboard/sections/<int:pk>/edit/', views.admin_section_edit, name='admin_section_edit'),
    path('dashboard/sections/<int:pk>/delete/', views.admin_section_delete, name='admin_section_delete'),
    
    # Help Requests / Yordam
    path('help/submit/', views.submit_help_request, name='submit_help_request'),
    path('dashboard/help_requests/', views.admin_help_requests, name='admin_help_requests'),
    path('dashboard/help-requests/<int:pk>/', views.admin_help_request_detail, name='admin_help_request_detail'),
    
    # Coin Management
    path('dashboard/coins/', views.admin_manage_coins, name='admin_manage_coins'),
    
    # Discounts / Chegirmalar
    path('dashboard/discounts/', views.admin_discounts, name='admin_discounts'),
    path('dashboard/discounts/add/', views.admin_discount_add, name='admin_discount_add'),
    path('dashboard/discounts/<int:pk>/edit/', views.admin_discount_edit, name='admin_discount_edit'),
    path('dashboard/discounts/<int:pk>/delete/', views.admin_discount_delete, name='admin_discount_delete'),
    
    # Qurilmalar boshqaruvi (User)
    path('profile/devices/', views.my_devices, name='my_devices'),
    path('profile/devices/<int:pk>/remove/', views.remove_device, name='remove_device'),
    path('profile/devices/<int:pk>/logout/', views.logout_device, name='logout_device'),
    path('profile/devices/<int:pk>/trust/', views.trust_device, name='trust_device'),
    path('profile/devices/logout-all/', views.logout_all_devices, name='logout_all_devices'),
    
    # Admin: Qurilmalar
    path('dashboard/users/<int:pk>/devices/', views.admin_user_devices, name='admin_user_devices'),
    path('dashboard/users/<int:user_pk>/devices/<int:device_pk>/logout/', views.admin_logout_user_device, name='admin_logout_user_device'),
    path('dashboard/users/<int:pk>/devices/logout-all/', views.admin_logout_all_user_devices, name='admin_logout_all_user_devices'),
]
