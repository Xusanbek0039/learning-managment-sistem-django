from django.utils import timezone
from django.shortcuts import redirect
from django.contrib import messages


class UpdateLastActivityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        
        if request.user.is_authenticated:
            request.user.last_activity = timezone.now()
            request.user.save(update_fields=['last_activity'])
        
        return response


class BlockedUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.user.is_blocked:
            from django.contrib.auth import logout
            logout(request)
            messages.error(request, "Sizning akkauntingiz bloklangan. Admin bilan bog'laning.")
            return redirect('login')
        
        return self.get_response(request)
