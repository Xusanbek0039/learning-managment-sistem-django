import hashlib
from django.utils import timezone
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.contrib import messages
from datetime import timedelta


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def get_device_info(request):
    user_agent = request.META.get('HTTP_USER_AGENT', '')
    
    # Detect device type
    device_type = 'desktop'
    if 'Mobile' in user_agent or 'Android' in user_agent:
        if 'Tablet' in user_agent or 'iPad' in user_agent:
            device_type = 'tablet'
        else:
            device_type = 'mobile'
    elif 'Tablet' in user_agent or 'iPad' in user_agent:
        device_type = 'tablet'
    
    # Detect browser
    browser = 'Noma\'lum'
    if 'Chrome' in user_agent and 'Edg' not in user_agent:
        browser = 'Chrome'
    elif 'Firefox' in user_agent:
        browser = 'Firefox'
    elif 'Safari' in user_agent and 'Chrome' not in user_agent:
        browser = 'Safari'
    elif 'Edg' in user_agent:
        browser = 'Edge'
    elif 'Opera' in user_agent or 'OPR' in user_agent:
        browser = 'Opera'
    elif 'MSIE' in user_agent or 'Trident' in user_agent:
        browser = 'Internet Explorer'
    
    # Detect OS
    os_name = 'Noma\'lum'
    if 'Windows' in user_agent:
        os_name = 'Windows'
    elif 'Mac OS' in user_agent or 'Macintosh' in user_agent:
        os_name = 'macOS'
    elif 'Linux' in user_agent and 'Android' not in user_agent:
        os_name = 'Linux'
    elif 'Android' in user_agent:
        os_name = 'Android'
    elif 'iPhone' in user_agent or 'iPad' in user_agent:
        os_name = 'iOS'
    
    # Create device name
    device_name = f"{os_name} - {browser}"
    
    # Create unique device ID
    device_id = hashlib.md5(f"{user_agent}{get_client_ip(request)}".encode()).hexdigest()
    
    return {
        'device_id': device_id,
        'device_name': device_name,
        'device_type': device_type,
        'browser': browser,
        'os': os_name,
        'user_agent': user_agent,
    }


class SessionTimeoutMiddleware:
    SESSION_TIMEOUT = 2 * 60 * 60  # 2 soat (sekundlarda)
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        if request.user.is_authenticated:
            from .models import UserSession, UserDevice
            
            session_key = request.session.session_key
            
            if session_key:
                try:
                    user_session = UserSession.objects.get(
                        session_key=session_key,
                        user=request.user,
                        is_active=True
                    )
                    
                    # Check if session expired (2 hours)
                    if user_session.is_expired:
                        user_session.is_active = False
                        user_session.save()
                        logout(request)
                        messages.warning(request, "Sessiya muddati tugadi. Iltimos, qaytadan kiring.")
                        return redirect('login')
                    
                    # Update last activity
                    user_session.last_activity = timezone.now()
                    user_session.expires_at = timezone.now() + timedelta(seconds=self.SESSION_TIMEOUT)
                    user_session.save(update_fields=['last_activity', 'expires_at'])
                    
                except UserSession.DoesNotExist:
                    # Create new session if not exists
                    device_info = get_device_info(request)
                    ip_address = get_client_ip(request)
                    
                    # Get or create device
                    device, created = UserDevice.objects.get_or_create(
                        user=request.user,
                        device_id=device_info['device_id'],
                        defaults={
                            'device_name': device_info['device_name'],
                            'device_type': device_info['device_type'],
                            'browser': device_info['browser'],
                            'os': device_info['os'],
                            'ip_address': ip_address,
                        }
                    )
                    
                    if not created:
                        device.last_login = timezone.now()
                        device.ip_address = ip_address
                        device.save(update_fields=['last_login', 'ip_address'])
                    
                    # Create session
                    UserSession.objects.create(
                        user=request.user,
                        device=device,
                        session_key=session_key,
                        ip_address=ip_address,
                        expires_at=timezone.now() + timedelta(seconds=self.SESSION_TIMEOUT)
                    )
        
        response = self.get_response(request)
        return response
