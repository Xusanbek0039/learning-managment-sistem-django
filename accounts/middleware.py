from django.utils import timezone
from django.core.cache import cache
from django.core.management import call_command
from django.contrib.auth import logout
from django.contrib import messages
from django.shortcuts import redirect
from .models import CustomUser, PaymentStatus

class BirthdayCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        today = timezone.now().date()
        key = 'birthdays_checked_today'
        
        if cache.get(key) != str(today):
            try:
                cache.set(key, str(today), 86400)
                call_command('check_birthdays')
            except Exception as e:
                print(f"Error checking birthdays: {e}")
        
        response = self.get_response(request)
        return response

class UpdateLastActivityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        
        if request.user.is_authenticated:
            # Update last activity every 5 minutes
            now = timezone.now()
            last = request.user.last_activity
            if not last or (now - last).total_seconds() > 300:
                CustomUser.objects.filter(pk=request.user.pk).update(last_activity=now)
                
        return response

class BlockedUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.is_blocked:
            logout(request)
            messages.error(request, "Sizning hisobingiz bloklangan!")
            return redirect('login')
            
        response = self.get_response(request)
        return response

class PaymentCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.role == 'student':
            try:
                # We use specific exception handling to avoid crashes if table doesn't exist yet
                # or relation issues, though typically getting the related object is safe
                if hasattr(request.user, 'payment_status'):
                    status = request.user.payment_status
                    if status.is_paid and status.paid_until and status.paid_until < timezone.now().date():
                        status.is_paid = False
                        status.auto_blocked = True
                        status.save()
                        
                        user = request.user
                        user.is_blocked = True
                        user.save()
            except Exception:
                pass
                
        response = self.get_response(request)
        return response
