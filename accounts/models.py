from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class Profession(models.Model):
    name = models.CharField(max_length=200, verbose_name="Kasb nomi")
    photo = models.ImageField(upload_to='professions/', verbose_name="Rasm")
    description = models.TextField(verbose_name="Kasb haqida")
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    def student_count(self):
        return self.enrollments.filter(user__role='student').count()
    
    def teacher_count(self):
        return self.students.filter(role='teacher').count()
    
    class Meta:
        verbose_name = "Kasb"
        verbose_name_plural = "Kasblar"
        ordering = ['-created_at']


class Section(models.Model):
    profession = models.ForeignKey(Profession, on_delete=models.CASCADE, related_name='sections')
    title = models.CharField(max_length=200, verbose_name="Bo'lim nomi")
    description = models.TextField(blank=True, null=True, verbose_name="Tavsif")
    order = models.IntegerField(default=0, verbose_name="Tartib")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['order', 'created_at']
        verbose_name = "Bo'lim"
        verbose_name_plural = "Bo'limlar"
    
    def __str__(self):
        return f"{self.profession.name} - {self.title}"


from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone

class CustomUser(AbstractUser):

    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('teacher', "O'qituvchi"),
        ('student', "O'quvchi"),
    )

    # Basic info
    first_name = models.CharField(max_length=100, verbose_name="Ism")
    last_name = models.CharField(max_length=100, verbose_name="Familiya")
    phone = models.CharField(max_length=20, unique=True, verbose_name="Telefon raqam")

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='student',
        verbose_name="Rol"
    )

    photo = models.ImageField(
        upload_to='profiles/',
        blank=True,
        null=True,
        verbose_name="Profil rasmi"
    )

    profession = models.ForeignKey(
        'Profession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='students',
        verbose_name="Yo'nalish"
    )

    is_blocked = models.BooleanField(default=False, verbose_name="Bloklangan")
    last_activity = models.DateTimeField(null=True, blank=True, verbose_name="Oxirgi faollik")

    bio = models.TextField(blank=True, null=True, verbose_name="O'zi haqida")

    # Gamification
    coins = models.IntegerField(default=0, verbose_name="Coinlar")
    total_online_time = models.IntegerField(default=0, verbose_name="Jami online vaqt (daqiqa)")

    # Documents
    address = models.CharField(max_length=300, blank=True, null=True, verbose_name="Yashash manzili")
    birth_date = models.DateField(null=True, blank=True, verbose_name="Tug'ilgan kuni")

    id_card = models.FileField(
        upload_to='documents/id_cards/',
        blank=True,
        null=True,
        verbose_name="ID karta / Pasport"
    )

    birth_certificate = models.FileField(
        upload_to='documents/birth_certificates/',
        blank=True,
        null=True,
        verbose_name="Tug'ilganlik haqida guvohnoma"
    )

    # ---------------- PROPERTIES ----------------

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_teacher(self):
        return self.role == 'teacher'

    @property
    def is_student(self):
        return self.role == 'student'

    @property
    def is_online(self):
        if not self.last_activity:
            return False
        return (timezone.now() - self.last_activity).total_seconds() < 300

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_initials(self):
        first = self.first_name[0] if self.first_name else ''
        last = self.last_name[0] if self.last_name else ''
        return f"{first}{last}".upper()

    # ---------------- COINS ----------------

    def add_coins(self, amount: int, reason: str = ''):
        from coin.models import CoinTransaction

        if amount <= 0:
            return

        self.coins += amount
        self.save(update_fields=['coins'])

        CoinTransaction.objects.create(
            user=self,
            amount=amount,
            action='add',
            reason=reason
        )

    def remove_coins(self, amount: int, reason: str = ''):
        from coin.models import CoinTransaction

        if amount <= 0:
            return

        if self.coins < amount:
            return

        self.coins -= amount
        self.save(update_fields=['coins'])

        CoinTransaction.objects.create(
            user=self,
            amount=amount,
            action='remove',
            reason=reason
        )

    # ---------------- META ----------------

    class Meta:
        verbose_name = "Foydalanuvchi"
        verbose_name_plural = "Foydalanuvchilar"

    def __str__(self):
        return self.username


class CourseEnrollment(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='enrollments')
    profession = models.ForeignKey(Profession, on_delete=models.CASCADE, related_name='enrollments')
    enrolled_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['user', 'profession']
        verbose_name = "Kursga yozilish"
        verbose_name_plural = "Kursga yozilishlar"
    
    def __str__(self):
        return f"{self.user.full_name} - {self.profession.name}"


class Lesson(models.Model):
    LESSON_TYPES = (
        ('video', 'Video darslik'),
        ('homework', 'Uyga vazifa'),
        ('test', 'Test'),
    )
    
    profession = models.ForeignKey(Profession, on_delete=models.CASCADE, related_name='lessons')
    section = models.ForeignKey(Section, on_delete=models.SET_NULL, null=True, blank=True, related_name='lessons', verbose_name="Bo'lim")
    title = models.CharField(max_length=300, verbose_name="Dars nomi")
    lesson_type = models.CharField(max_length=20, choices=LESSON_TYPES, verbose_name="Dars turi")
    order = models.IntegerField(default=0, verbose_name="Tartib")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    
    class Meta:
        ordering = ['order', 'created_at']
        verbose_name = "Dars"
        verbose_name_plural = "Darslar"
    
    def __str__(self):
        return f"{self.title} ({self.get_lesson_type_display()})"


class VideoLesson(models.Model):
    lesson = models.OneToOneField(Lesson, on_delete=models.CASCADE, related_name='video')
    video_url = models.URLField(verbose_name="Video havolasi", blank=True, null=True, help_text="YouTube yoki Vimeo havolasi")
    youtube_url = models.URLField(verbose_name="YouTube havolasi", blank=True, null=True)
    duration = models.IntegerField(default=0, verbose_name="Davomiyligi (daqiqa)")
    
    @property
    def has_video(self):
        return bool(self.video_url) or bool(self.youtube_url)
    
    @property
    def is_vimeo(self):
        url = self.video_url or ''
        return 'vimeo.com' in url
    
    @property
    def is_youtube(self):
        url = self.video_url or self.youtube_url or ''
        return 'youtube.com' in url or 'youtu.be' in url
    
    def get_video_id(self):
        url = self.video_url or self.youtube_url or ''
        
        # Vimeo: https://vimeo.com/123456789 or https://vimeo.com/123456789?share=copy
        # or https://player.vimeo.com/video/123456789
        if 'vimeo.com' in url:
            if '/video/' in url:
                video_id = url.split('/video/')[1].split('?')[0].split('/')[0]
            else:
                # Extract number after vimeo.com/
                import re
                match = re.search(r'vimeo\.com/(\d+)', url)
                if match:
                    video_id = match.group(1)
                else:
                    video_id = url.split('vimeo.com/')[1].split('?')[0].split('/')[0]
            return video_id
        
        # YouTube formats:
        # https://www.youtube.com/watch?v=VIDEO_ID
        # https://youtu.be/VIDEO_ID
        # https://www.youtube.com/embed/VIDEO_ID?si=xxx
        # https://youtube.com/embed/VIDEO_ID
        if 'youtube.com' in url or 'youtu.be' in url:
            import re
            # Try embed format first
            if '/embed/' in url:
                match = re.search(r'/embed/([a-zA-Z0-9_-]+)', url)
                if match:
                    return match.group(1)
            # Try watch?v= format
            if 'watch?v=' in url:
                match = re.search(r'watch\?v=([a-zA-Z0-9_-]+)', url)
                if match:
                    return match.group(1)
            # Try youtu.be format
            if 'youtu.be/' in url:
                match = re.search(r'youtu\.be/([a-zA-Z0-9_-]+)', url)
                if match:
                    return match.group(1)
        
        return None
    
    def get_embed_url(self):
        video_id = self.get_video_id()
        if not video_id:
            return None
        
        if self.is_vimeo:
            return f"https://player.vimeo.com/video/{video_id}?autoplay=1&title=0&byline=0&portrait=0"
        else:
            return f"https://www.youtube-nocookie.com/embed/{video_id}?autoplay=1&rel=0&modestbranding=1"
    
    def get_youtube_url(self):
        """Original YouTube URL for 'Watch on YouTube' button"""
        url = self.youtube_url or self.video_url or ''
        if 'youtube.com' in url or 'youtu.be' in url:
            return url
        return None
    
    def get_thumbnail(self):
        video_id = self.get_video_id()
        if not video_id:
            return None
        
        if self.is_vimeo:
            # Vimeo thumbnails need API call, use placeholder
            return None
        else:
            return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    
    def __str__(self):
        return self.lesson.title


class VideoProgress(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    video = models.ForeignKey(VideoLesson, on_delete=models.CASCADE)
    watched = models.BooleanField(default=False)
    watched_at = models.DateTimeField(null=True, blank=True)
    coin_awarded = models.BooleanField(default=False)
    
    class Meta:
        unique_together = ['user', 'video']


class Homework(models.Model):
    lesson = models.OneToOneField(Lesson, on_delete=models.CASCADE, related_name='homework')
    description = models.TextField(verbose_name="Vazifa tavsifi")
    due_date = models.DateTimeField(null=True, blank=True, verbose_name="Topshirish muddati")
    max_score = models.IntegerField(default=100, verbose_name="Maksimal ball")
    late_penalty = models.IntegerField(default=10, verbose_name="Kechikish jarimasi (%)")
    allow_late = models.BooleanField(default=True, verbose_name="Kech topshirishga ruxsat")
    
    def __str__(self):
        return self.lesson.title
    
    @property
    def is_deadline_passed(self):
        if self.due_date:
            from django.utils import timezone
            return timezone.now() > self.due_date
        return False


class HomeworkSubmission(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Kutilmoqda'),
        ('reviewing', 'Ko\'rib chiqilmoqda'),
        ('revision', 'Qayta ishlash kerak'),
        ('accepted', 'Qabul qilindi'),
        ('graded', 'Baholangan'),
    )
    
    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name='submissions')
    student = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='homework_submissions')
    file = models.FileField(upload_to='homework_submissions/')
    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    grade = models.IntegerField(null=True, blank=True, verbose_name="Baho (1-100)")
    feedback = models.TextField(blank=True, null=True, verbose_name="Izoh")
    feedback_file = models.FileField(upload_to='homework_feedback/', null=True, blank=True, verbose_name="Izoh fayli")
    graded_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='graded_homeworks')
    graded_at = models.DateTimeField(null=True, blank=True)
    coin_awarded = models.BooleanField(default=False)
    is_late = models.BooleanField(default=False, verbose_name="Kech topshirilgan")
    revision_count = models.IntegerField(default=0, verbose_name="Qayta topshirish soni")
    version = models.IntegerField(default=1, verbose_name="Versiya")
    
    class Meta:
        ordering = ['-submitted_at']
    
    def __str__(self):
        return f"{self.student.full_name} - {self.homework.lesson.title}"
    
    @property
    def unread_messages_count(self):
        return self.messages.filter(is_read=False).exclude(sender=self.student).count()
    
    def calculate_final_grade(self):
        if self.grade and self.is_late and self.homework.late_penalty:
            penalty = self.grade * self.homework.late_penalty / 100
            return max(0, int(self.grade - penalty))
        return self.grade


class HomeworkMessage(models.Model):
    submission = models.ForeignKey(HomeworkSubmission, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='homework_messages')
    message = models.TextField(verbose_name="Xabar")
    file = models.FileField(upload_to='homework_chat/', null=True, blank=True, verbose_name="Fayl")
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['created_at']
    
    def __str__(self):
        return f"{self.sender.full_name}: {self.message[:30]}"


class HomeworkRevision(models.Model):
    submission = models.ForeignKey(HomeworkSubmission, on_delete=models.CASCADE, related_name='revisions')
    file = models.FileField(upload_to='homework_revisions/')
    submitted_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True, null=True, verbose_name="Izoh")
    
    class Meta:
        ordering = ['-submitted_at']
    
    def __str__(self):
        return f"Revision {self.pk} - {self.submission}"


class Test(models.Model):
    TEST_TYPES = (
        ('lesson', 'Dars yuzasidan'),
        ('midterm', 'Oraliq test'),
        ('practice', 'Tajriba oshirish'),
    )
    
    lesson = models.OneToOneField(Lesson, on_delete=models.CASCADE, related_name='test')
    test_type = models.CharField(max_length=20, choices=TEST_TYPES, default='lesson', verbose_name="Test turi")
    time_limit = models.IntegerField(default=30, verbose_name="Vaqt chegarasi (daqiqa)")
    passing_score = models.IntegerField(default=60, verbose_name="O'tish bali (%)")
    allow_retry = models.BooleanField(default=False, verbose_name="Qayta ishlashga ruxsat")
    
    def __str__(self):
        return self.lesson.title


class TestQuestion(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name='questions')
    question_text = models.TextField(verbose_name="Savol matni")
    question_image = models.ImageField(upload_to='test_questions/', blank=True, null=True, verbose_name="Savol rasmi")
    order = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['order']
    
    def __str__(self):
        return self.question_text[:50]


class TestAnswer(models.Model):
    question = models.ForeignKey(TestQuestion, on_delete=models.CASCADE, related_name='answers')
    answer_text = models.CharField(max_length=500, verbose_name="Javob matni")
    is_correct = models.BooleanField(default=False, verbose_name="To'g'ri javob")
    
    def __str__(self):
        return self.answer_text


class TestResult(models.Model):
    test = models.ForeignKey(Test, on_delete=models.CASCADE, related_name='results')
    student = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='test_results')
    score = models.IntegerField(verbose_name="Ball")
    total_questions = models.IntegerField()
    correct_answers = models.IntegerField()
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(auto_now_add=True)
    passed = models.BooleanField(default=False)
    coin_awarded = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-completed_at']
    
    def __str__(self):
        return f"{self.student.full_name} - {self.test.lesson.title}: {self.score}%"


class TestUserAnswer(models.Model):
    result = models.ForeignKey(TestResult, on_delete=models.CASCADE, related_name='user_answers')
    question = models.ForeignKey(TestQuestion, on_delete=models.CASCADE)
    selected_answer = models.ForeignKey(TestAnswer, on_delete=models.CASCADE, null=True, blank=True)
    is_correct = models.BooleanField(default=False)


class Certificate(models.Model):
    student = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='certificates')
    profession = models.ForeignKey(Profession, on_delete=models.CASCADE, related_name='certificates')
    file = models.FileField(upload_to='certificates/')
    issued_at = models.DateTimeField(auto_now_add=True)
    issued_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='issued_certificates')
    
    class Meta:
        ordering = ['-issued_at']
    
    def __str__(self):
        return f"{self.student.full_name} - {self.profession.name}"


class Message(models.Model):
    MESSAGE_TYPES = (
        ('all', 'Barchaga'),
        ('students', 'O\'quvchilarga'),
        ('teachers', 'O\'qituvchilarga'),
        ('personal', 'Shaxsiy'),
        ('system', 'Tizim xabari'),
        ('payment', 'To\'lov eslatmasi'),
        ('motivation', 'Rag\'batlantirish'),
        ('warning', 'Ogohlantirish'),
        ('achievement', 'Yutuq'),
        ('reminder', 'Eslatma'),
        ('recommendation', 'Tavsiya'),
        ('security', 'Xavfsizlik'),
    )
    
    title = models.CharField(max_length=200, verbose_name="Sarlavha")
    content = models.TextField(verbose_name="Xabar matni")
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPES, default='all')
    recipient = models.ForeignKey(CustomUser, on_delete=models.CASCADE, null=True, blank=True, related_name='received_messages')
    sender = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_messages')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Xabar"
        verbose_name_plural = "Xabarlar"
    
    def __str__(self):
        return self.title


class PaymentStatus(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='payment_status')
    is_paid = models.BooleanField(default=False, verbose_name="To'langan")
    paid_until = models.DateField(null=True, blank=True, verbose_name="To'lov muddati")
    last_payment_date = models.DateField(null=True, blank=True)
    auto_blocked = models.BooleanField(default=False, verbose_name="Avtomatik bloklangan")
    
    class Meta:
        verbose_name = "To'lov holati"
        verbose_name_plural = "To'lov holatlari"
    
    def __str__(self):
        status = "To'langan" if self.is_paid else "To'lanmagan"
        return f"{self.user.full_name} - {status}"


class HelpRequest(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Kutilmoqda'),
        ('in_progress', 'Ko\'rib chiqilmoqda'),
        ('resolved', 'Hal qilindi'),
        ('closed', 'Yopildi'),
    )
    
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='help_requests')
    subject = models.CharField(max_length=200, verbose_name="Mavzu")
    message = models.TextField(verbose_name="Xabar")
    image = models.ImageField(upload_to='help_requests/', blank=True, null=True, verbose_name="Rasm")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="Holat")
    admin_response = models.TextField(blank=True, null=True, verbose_name="Admin javobi")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Yordam so'rovi"
        verbose_name_plural = "Yordam so'rovlari"
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.user.full_name} - {self.subject}"


class UserDevice(models.Model):
    user = models.ForeignKey('CustomUser', on_delete=models.CASCADE, related_name='devices')
    device_id = models.CharField(max_length=255, verbose_name="Qurilma ID")
    device_name = models.CharField(max_length=200, verbose_name="Qurilma nomi")
    device_type = models.CharField(max_length=50, verbose_name="Qurilma turi")  # mobile, tablet, desktop
    browser = models.CharField(max_length=100, blank=True, null=True, verbose_name="Brauzer")
    os = models.CharField(max_length=100, blank=True, null=True, verbose_name="Operatsion tizim")
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="IP manzil")
    location = models.CharField(max_length=200, blank=True, null=True, verbose_name="Joylashuv")
    is_trusted = models.BooleanField(default=False, verbose_name="Ishonchli qurilma")
    first_login = models.DateTimeField(auto_now_add=True, verbose_name="Birinchi kirish")
    last_login = models.DateTimeField(auto_now=True, verbose_name="Oxirgi kirish")
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    
    class Meta:
        unique_together = ['user', 'device_id']
        ordering = ['-last_login']
        verbose_name = "Foydalanuvchi qurilmasi"
        verbose_name_plural = "Foydalanuvchi qurilmalari"
    
    def __str__(self):
        return f"{self.user.username} - {self.device_name}"


class UserSession(models.Model):
    user = models.ForeignKey('CustomUser', on_delete=models.CASCADE, related_name='sessions')
    device = models.ForeignKey(UserDevice, on_delete=models.CASCADE, related_name='sessions', null=True, blank=True)
    session_key = models.CharField(max_length=255, unique=True, verbose_name="Session kaliti")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True, verbose_name="Boshlangan vaqt")
    last_activity = models.DateTimeField(auto_now=True, verbose_name="Oxirgi faollik")
    expires_at = models.DateTimeField(verbose_name="Tugash vaqti")
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    
    class Meta:
        ordering = ['-last_activity']
        verbose_name = "Foydalanuvchi sessiyasi"
        verbose_name_plural = "Foydalanuvchi sessiyalari"
    
    def __str__(self):
        return f"{self.user.username} - {self.started_at}"
    
    @property
    def is_expired(self):
        return timezone.now() > self.expires_at


class HTMLDeploy(models.Model):
    DEPLOY_TYPES = (
        ('html', 'HTML fayl'),
        ('project', 'Loyiha (ZIP)'),
    )
    
    user = models.ForeignKey('CustomUser', on_delete=models.CASCADE, related_name='deploys')
    profession = models.ForeignKey('Profession', on_delete=models.CASCADE, related_name='deploys', null=True, blank=True)
    title = models.CharField(max_length=200, verbose_name="Loyiha nomi")
    deploy_type = models.CharField(max_length=20, choices=DEPLOY_TYPES, default='html', verbose_name="Turi")
    folder_name = models.CharField(max_length=100, verbose_name="Papka nomi", default='project')
    entry_file = models.CharField(max_length=100, default='index.html', verbose_name="Asosiy fayl")
    html_content = models.TextField(blank=True, null=True, verbose_name="HTML kodi")
    description = models.TextField(blank=True, null=True, verbose_name="Tavsif")
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    views_count = models.IntegerField(default=0, verbose_name="Ko'rishlar soni")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['user', 'folder_name']
        ordering = ['-created_at']
        verbose_name = "HTML Deploy"
        verbose_name_plural = "HTML Deploylar"
    
    def __str__(self):
        return f"{self.user.username}/{self.folder_name}"
    
    def get_url(self):
        if self.deploy_type == 'project':
            return f"/coding/{self.user.username}/{self.folder_name}/{self.entry_file}"
        return f"/coding/{self.user.username}/{self.folder_name}"
    
    def get_folder_path(self):
        import os
        from django.conf import settings
        return os.path.join(settings.MEDIA_ROOT, 'deploys', self.user.username, self.folder_name)
    
    @property
    def full_url(self):
        return self.get_url()


class Discount(models.Model):
    DISCOUNT_TYPES = (
        ('percentage', 'Foiz'),
        ('fixed', 'Qat\'iy summa'),
    )
    
    name = models.CharField(max_length=200, verbose_name="Chegirma nomi")
    description = models.TextField(blank=True, null=True, verbose_name="Tavsif")
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPES, default='percentage', verbose_name="Chegirma turi")
    discount_value = models.IntegerField(verbose_name="Chegirma qiymati")
    min_coins_required = models.IntegerField(default=0, verbose_name="Minimal coin talab")
    profession = models.ForeignKey('Profession', on_delete=models.CASCADE, null=True, blank=True, related_name='discounts', verbose_name="Kurs")
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    valid_from = models.DateTimeField(null=True, blank=True, verbose_name="Boshlanish sanasi")
    valid_until = models.DateTimeField(null=True, blank=True, verbose_name="Tugash sanasi")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = "Chegirma"
        verbose_name_plural = "Chegirmalar"
    
    def __str__(self):
        return self.name
    
    @property
    def is_valid(self):
        now = timezone.now()
        if not self.is_active:
            return False
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        return True


class SystemReport(models.Model):
    REPORT_TYPES = (
        ('daily', 'Kunlik'),
        ('weekly', 'Haftalik'),
        ('monthly', 'Oylik'),
        ('custom', 'Maxsus'),
    )
    
    title = models.CharField(max_length=200, verbose_name="Hisobot nomi")
    report_type = models.CharField(max_length=20, choices=REPORT_TYPES, default='monthly', verbose_name="Hisobot turi")
    date_from = models.DateField(verbose_name="Boshlanish sanasi")
    date_to = models.DateField(verbose_name="Tugash sanasi")
    
    # Statistika ma'lumotlari
    total_users = models.IntegerField(default=0)
    total_students = models.IntegerField(default=0)
    total_teachers = models.IntegerField(default=0)
    new_users = models.IntegerField(default=0, verbose_name="Yangi foydalanuvchilar")
    
    total_courses = models.IntegerField(default=0)
    total_lessons = models.IntegerField(default=0)
    total_videos = models.IntegerField(default=0)
    total_tests = models.IntegerField(default=0)
    total_homeworks = models.IntegerField(default=0)
    
    new_enrollments = models.IntegerField(default=0, verbose_name="Yangi yozilishlar")
    test_submissions = models.IntegerField(default=0, verbose_name="Test topshiruvlar")
    homework_submissions = models.IntegerField(default=0, verbose_name="Vazifa topshiruvlar")
    video_views = models.IntegerField(default=0, verbose_name="Video ko'rishlar")
    
    total_deploys = models.IntegerField(default=0)
    new_deploys = models.IntegerField(default=0, verbose_name="Yangi loyihalar")
    deploy_views = models.IntegerField(default=0)
    
    total_coins = models.IntegerField(default=0)
    coins_earned = models.IntegerField(default=0, verbose_name="Ishlangan coinlar")
    coins_spent = models.IntegerField(default=0, verbose_name="Sarflangan coinlar")
    
    total_certificates = models.IntegerField(default=0)
    new_certificates = models.IntegerField(default=0, verbose_name="Yangi sertifikatlar")
    
    total_activities = models.IntegerField(default=0, verbose_name="Jami harakatlar")
    
    # PDF fayl
    pdf_file = models.FileField(upload_to='reports/', null=True, blank=True, verbose_name="PDF fayl")
    
    # Meta
    created_by = models.ForeignKey('CustomUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_reports')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-date_to', '-created_at']
        verbose_name = "Tizim hisoboti"
        verbose_name_plural = "Tizim hisobotlari"
    
    def __str__(self):
        return f"{self.title} ({self.date_from} - {self.date_to})"



