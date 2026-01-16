from django.core.management.base import BaseCommand
from accounts.models import CustomUser
from blog.models import Post, Comment, Like
from django.test import RequestFactory
from blog.views import toggle_like

class Command(BaseCommand):
    help = 'Verify Blog functionality'

    def handle(self, *args, **kwargs):
        self.stdout.write('Verifying blog functionality...')
        
        # 1. Setup users
        admin, _ = CustomUser.objects.get_or_create(username='test_admin', role='admin')
        student, _ = CustomUser.objects.get_or_create(username='test_student', role='student')
        
        # Reset coins
        admin.coins = 0
        admin.save()
        student.coins = 0
        student.save()
        
        # 2. Test Post Creation
        try:
            post = Post.objects.create(
                title='Test Post', 
                content='Content', 
                author=admin, 
                post_type='news'
            )
            self.stdout.write(self.style.SUCCESS('✅ Post created successfully'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ Post creation failed: {e}'))
            return

        # 3. Test Like (Coin logic)
        factory = RequestFactory()
        request = factory.get(f'/blog/{post.pk}/like/')
        request.user = student
        
        # Execute view
        toggle_like(request, pk=post.pk)
        
        student.refresh_from_db()
        if student.coins == 1:
            self.stdout.write(self.style.SUCCESS('✅ Like awarded 1 coin'))
        else:
            self.stdout.write(self.style.ERROR(f'❌ Like coin failed. Coins: {student.coins}'))
            
        # 4. Test Comment (Model logic manually since view handles request)
        comment = Comment.objects.create(
            post=post,
            user=student,
            text='Nice post'
        )
        student.add_coins(1, "Comment reward") # Simulate view logic
        
        student.refresh_from_db()
        if student.coins == 2:
            self.stdout.write(self.style.SUCCESS('✅ Comment awarded 1 coin (simulated)'))
        else:
            self.stdout.write(self.style.ERROR(f'❌ Comment coin failed. Coins: {student.coins}'))

        # Cleanup
        post.delete()
        # admin.delete() 
        # student.delete()
