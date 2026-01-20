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


class CustomUser(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('teacher', 'O\'qituvchi'),
        ('student', 'O\'quvchi'),
    )
    
    first_name = models.CharField(max_length=100, verbose_name="Ism")
    last_name = models.CharField(max_length=100, verbose_name="Familiya")
    phone = models.CharField(max_length=20, unique=True, verbose_name="Telefon raqam")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='student', verbose_name="Rol")
    photo = models.ImageField(upload_to='profiles/', blank=True, null=True, verbose_name="Profil rasmi")
    profession = models.ForeignKey(Profession, on_delete=models.SET_NULL, null=True, blank=True, related_name='students', verbose_name="Yo'nalish")
    is_blocked = models.BooleanField(default=False, verbose_name="Bloklangan")
    last_activity = models.DateTimeField(null=True, blank=True, verbose_name="Oxirgi faollik")
    bio = models.TextField(blank=True, null=True, verbose_name="O'zi haqida")
    coins = models.IntegerField(default=0, verbose_name="Coinlar")
    total_online_time = models.IntegerField(default=0, verbose_name="Jami online vaqt (daqiqa)")
    
    # New fields
    address = models.CharField(max_length=300, blank=True, null=True, verbose_name="Yashash manzili")
    birth_date = models.DateField(null=True, blank=True, verbose_name="Tug'ilgan kuni")
    id_card = models.FileField(upload_to='documents/id_cards/', blank=True, null=True, verbose_name="ID karta/Pasport")
    birth_certificate = models.FileField(upload_to='documents/birth_certificates/', blank=True, null=True, verbose_name="Tug'ilganlik haqida guvohnoma")
    
    def __str__(self):
        return self.username
    
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
        if self.last_activity:
            now = timezone.now()
            diff = now - self.last_activity
            return diff.total_seconds() < 300
        return False
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
    
    def get_initials(self):
        first = self.first_name[0] if self.first_name else ''
        last = self.last_name[0] if self.last_name else ''
        return f"{first}{last}".upper()
    
    def add_coins(self, amount, reason=''):
        from coin.models import CoinTransaction
        self.coins += amount
        self.save()
        CoinTransaction.objects.create(user=self, amount=amount, reason=reason)
    
    class Meta:
        verbose_name = "Foydalanuvchi"
        verbose_name_plural = "Foydalanuvchilar"


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
    
    def __str__(self):
        return self.lesson.title


class HomeworkSubmission(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Tekshirilmoqda'),
        ('graded', 'Baholangan'),
    )
    
    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name='submissions')
    student = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='homework_submissions')
    file = models.FileField(upload_to='homework_submissions/')
    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    grade = models.IntegerField(null=True, blank=True, verbose_name="Baho (1-100)")
    feedback = models.TextField(blank=True, null=True, verbose_name="Izoh")
    graded_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='graded_homeworks')
    graded_at = models.DateTimeField(null=True, blank=True)
    coin_awarded = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-submitted_at']
    
    def __str__(self):
        return f"{self.student.full_name} - {self.homework.lesson.title}"


class Test(models.Model):
    lesson = models.OneToOneField(Lesson, on_delete=models.CASCADE, related_name='test')
    time_limit = models.IntegerField(default=30, verbose_name="Vaqt chegarasi (daqiqa)")
    passing_score = models.IntegerField(default=60, verbose_name="O'tish bali (%)")
    
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



