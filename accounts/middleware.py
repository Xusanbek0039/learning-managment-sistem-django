from django.utils import timezone
from django.shortcuts import redirect
from django.contrib import messages as django_messages


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
            django_messages.error(request, "Sizning akkauntingiz bloklangan. To'lov uchun admin bilan bog'laning.")
            return redirect('login')
        
        return self.get_response(request)


class PaymentCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._last_check_date = None

    def __call__(self, request):
        today = timezone.now().date()
        
        # Kuniga bir marta tekshirish
        if self._last_check_date != today:
            self._last_check_date = today
            self._check_payments(today)
        
        return self.get_response(request)
    
    def _check_payments(self, today):
        from .models import CustomUser, Message, PaymentStatus
        
        day = today.day
        
        # 5-sanada to'lov eslatmasi
        if day == 5:
            students = CustomUser.objects.filter(role='student', is_blocked=False)
            for student in students:
                # Bu oy uchun xabar yuborilganmi tekshirish
                existing = Message.objects.filter(
                    recipient=student,
                    message_type='payment',
                    created_at__month=today.month,
                    created_at__year=today.year
                ).exists()
                
                if not existing:
                    Message.objects.create(
                        title="⚠️ To'lov eslatmasi",
                        content="Hurmatli o'quvchi! Iltimos, oylik to'lovingizni 10-sanagacha amalga oshiring. Aks holda hisobingiz vaqtincha bloklanadi.",
                        message_type='payment',
                        recipient=student
                    )
        
        # 10-sanada avtomatik bloklash
        if day == 10:
            students = CustomUser.objects.filter(role='student', is_blocked=False)
            for student in students:
                # To'lov holatini tekshirish
                payment_status, created = PaymentStatus.objects.get_or_create(user=student)
                
                if not payment_status.is_paid:
                    student.is_blocked = True
                    student.save()
                    payment_status.auto_blocked = True
                    payment_status.save()
                    
                    Message.objects.create(
                        title="🔒 Hisobingiz bloklandi",
                        content="To'lov amalga oshirilmaganligi sababli hisobingiz vaqtincha bloklandi. To'lovni amalga oshirgach admin bilan bog'laning.",
                        message_type='system',
                        recipient=student
                    )
        
        # Oyning 1-sanasida yangi oy uchun to'lov holatini yangilash
        if day == 1:
            PaymentStatus.objects.all().update(is_paid=False, auto_blocked=False)
