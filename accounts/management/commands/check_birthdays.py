from django.core.management.base import BaseCommand
from django.utils import timezone
from accounts.models import CustomUser, Message

class Command(BaseCommand):
    help = 'Check for birthdays and send messages'

    def handle(self, *args, **kwargs):
        today = timezone.now().date()
        users = CustomUser.objects.filter(birth_date__day=today.day, birth_date__month=today.month)
        
        if not users.exists():
            self.stdout.write(self.style.SUCCESS('No birthdays today.'))
            return

        count = 0
        for user in users:
            # 1. Personal message
            already_sent_personal = Message.objects.filter(
                recipient=user,
                message_type='personal',
                title__contains="Tug'ilgan kuningiz bilan",
                created_at__date=today
            ).exists()
            
            if not already_sent_personal:
                Message.objects.create(
                    recipient=user,
                    message_type='personal',
                    title="Tug'ilgan kuningiz bilan! 🎂",
                    content=f"Hurmatli {user.first_name}, sizni tug'ilgan kuningiz bilan tabriklaymiz! Sizga sog'-salomatlik, o'qishlaringizda omad tilaymiz!",
                    sender=None # System
                )
                self.stdout.write(f"Sent personal message to {user.username}")
                count += 1
            
            # 2. Public message (to everyone)
            already_sent_public = Message.objects.filter(
                message_type='all',
                content__contains=user.full_name,
                title__contains="tug'ilgan kun",
                created_at__date=today
            ).exists()
            
            if not already_sent_public:
                Message.objects.create(
                    recipient=None,
                    message_type='all',
                    title="Bugun tug'ilgan kun! 🎉",
                    content=f"Hurmatli foydalanuvchilar! Bugun {user.full_name}ning tug'ilgan kuni. Shu sababdan uni jamoamiz nomidan chin qalbimizdan tabriklaymiz!",
                    sender=None
                )
                self.stdout.write(f"Sent public message about {user.username}")
                count += 1
                
        self.stdout.write(self.style.SUCCESS(f'Successfully processed birthdays. Sent {count} messages.'))
