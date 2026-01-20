# Generated manually for model updates

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('ai_yordamchi', '0001_initial'),
    ]

    operations = [
        # Add new fields to ChatSession
        migrations.AddField(
            model_name='chatsession',
            name='title',
            field=models.CharField(default='Yangi suhbat', max_length=200),
        ),
        migrations.AddField(
            model_name='chatsession',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AddField(
            model_name='chatsession',
            name='is_active',
            field=models.BooleanField(default=True),
        ),
        
        # Update ChatSession related_name
        migrations.AlterField(
            model_name='chatsession',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chat_sessions', to=settings.AUTH_USER_MODEL),
        ),
        
        # Add ChatSession Meta
        migrations.AlterModelOptions(
            name='chatsession',
            options={'ordering': ['-updated_at'], 'verbose_name': 'Suhbat', 'verbose_name_plural': 'Suhbatlar'},
        ),
        
        # Rename chat to session in ChatMessage
        migrations.RenameField(
            model_name='chatmessage',
            old_name='chat',
            new_name='session',
        ),
        
        # Rename text to content in ChatMessage
        migrations.RenameField(
            model_name='chatmessage',
            old_name='text',
            new_name='content',
        ),
        
        # Update ChatMessage content field
        migrations.AlterField(
            model_name='chatmessage',
            name='content',
            field=models.TextField(),
        ),
        
        # Remove file field, add image field
        migrations.RemoveField(
            model_name='chatmessage',
            name='file',
        ),
        migrations.AddField(
            model_name='chatmessage',
            name='image',
            field=models.ImageField(blank=True, null=True, upload_to='chat_images/'),
        ),
        
        # Add coins_spent field
        migrations.AddField(
            model_name='chatmessage',
            name='coins_spent',
            field=models.IntegerField(default=0),
        ),
        
        # Update role choices
        migrations.AlterField(
            model_name='chatmessage',
            name='role',
            field=models.CharField(choices=[('user', 'Foydalanuvchi'), ('assistant', 'AI Yordamchi')], max_length=20),
        ),
        
        # Add ChatMessage Meta
        migrations.AlterModelOptions(
            name='chatmessage',
            options={'ordering': ['created_at'], 'verbose_name': 'Xabar', 'verbose_name_plural': 'Xabarlar'},
        ),
    ]
