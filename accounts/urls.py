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
    path('dashboard/export-pdf/', views.admin_export_pdf, name='admin_export_pdf'),
]
