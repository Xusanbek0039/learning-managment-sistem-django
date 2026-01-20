from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.conf import settings
from django.core.paginator import Paginator
import base64

from .models import ChatSession, ChatMessage
from coin.models import CoinTransaction, ActivityLog


def get_openai_client():
    """Get OpenAI client if API key is configured."""
    if not getattr(settings, 'OPENAI_API_KEY', None):
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=settings.OPENAI_API_KEY, timeout=30)
    except Exception:
        return None


@login_required
def chat_home(request):
    """List user's chat sessions."""
    sessions = ChatSession.objects.filter(user=request.user, is_active=True)
    return render(request, 'ai_yordamchi/chat_home.html', {
        'sessions': sessions,
    })


@login_required
def new_session(request):
    """Create a new chat session."""
    session = ChatSession.objects.create(user=request.user)
    return redirect('ai_yordamchi:chat_session', pk=session.pk)


@login_required
def chat_session(request, pk):
    """Chat interface with a session."""
    session = get_object_or_404(ChatSession, pk=pk, user=request.user)
    chat_messages = session.messages.all()
    
    return render(request, 'ai_yordamchi/chat_session.html', {
        'session': session,
        'chat_messages': chat_messages,
        'user_coins': request.user.coins,
    })


@login_required
@require_POST
def send_message(request, pk):
    """Send message to AI and get response."""
    session = get_object_or_404(ChatSession, pk=pk, user=request.user)
    
    content = request.POST.get('content', '').strip()
    image = request.FILES.get('image')
    
    if not content and not image:
        messages.error(request, "Xabar yoki rasm yuborishingiz kerak.")
        return redirect('ai_yordamchi:chat_session', pk=pk)
    
    # Check coins
    if request.user.coins < 1:
        messages.error(request, "Sizda yetarli coin yo'q. AI bilan suhbatlashish uchun 1 coin kerak.")
        return redirect('ai_yordamchi:chat_session', pk=pk)
    
    # Save user message
    user_message = ChatMessage.objects.create(
        session=session,
        role='user',
        content=content,
        image=image,
        coins_spent=1,
    )
    
    # Deduct coin
    request.user.coins -= 1
    request.user.save()
    
    # Log transaction
    CoinTransaction.objects.create(
        user=request.user,
        amount=-1,
        reason="AI yordamchi bilan suhbat"
    )
    
    # Log activity
    ActivityLog.objects.create(
        user=request.user,
        action_type='earn_coins',
        description="AI yordamchi bilan suhbatlashdi (-1 coin)",
        ip_address=get_client_ip(request)
    )
    
    # Update session title if first message
    if session.messages.count() == 1 and content:
        session.title = content[:50] + ('...' if len(content) > 50 else '')
        session.save()
    
    # Get AI response
    client = get_openai_client()
    
    if not client:
        ai_response = "⚠️ AI xizmati hozirda mavjud emas. Iltimos, keyinroq urinib ko'ring."
    else:
        try:
            # Build messages history
            system_prompt = """Siz LMS o'quv platformasining AI yordamchisisiz. O'zbek tilida javob bering.

JAVOB FORMATI:

1. STRUKTURA:
   - Avval qisqa tushuntirish (1-2 gap)
   - Keyin kod bloklari
   - Oxirida "Qanday ishlatish" bo'limi

2. KOD BLOKLARI:
   - Har doim Markdown code block ishlatilsin
   - Code block boshida til nomi bo'lsin: ```python, ```html, ```css, ```javascript
   - Kod ichida oddiy matn bo'lmasin
   - Izohlar faqat kod tashqarisida

3. KO'P FAYLLAR:
   - Har bir fayl alohida sarlavha bilan:
   
   **HTML (index.html)**
   ```html
   <h1>Salom</h1>
   ```
   
   **CSS (style.css)**
   ```css
   h1 { color: red; }
   ```

4. FORMATLASH:
   - Havolalar: [matn](URL)
   - Ro'yxatlar: - yoki 1. 2. 3.
   - Muhim so'zlar: **qalin**

5. USLUB:
   - Boshlovchilar uchun tushunarli
   - Do'stona va rag'batlantiruvchi
   - Qisqa va aniq

6. TAQIQLANGAN:
   - < yoki > belgilarini escape qilish
   - \\n yoki \\u000A ko'rinishlar
   - Kod ichida tushuntirish yozish"""
            
            history = [
                {"role": "system", "content": system_prompt}
            ]
            
            for msg in session.messages.all():
                if msg.role == 'user':
                    if msg.image:
                        # Handle image message
                        try:
                            image_data = base64.b64encode(msg.image.read()).decode('utf-8')
                            msg.image.seek(0)
                            content_parts = []
                            if msg.content:
                                content_parts.append({"type": "text", "text": msg.content})
                            content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}
                            })
                            history.append({"role": "user", "content": content_parts})
                        except Exception:
                            if msg.content:
                                history.append({"role": "user", "content": msg.content})
                    else:
                        if msg.content:
                            history.append({"role": "user", "content": msg.content})
                else:
                    if msg.content:
                        history.append({"role": "assistant", "content": msg.content})
            
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=history,
                max_tokens=2000,
            )
            
            ai_response = response.choices[0].message.content
            
        except Exception as e:
            import traceback
            print(f"OpenAI Error: {str(e)}")
            print(traceback.format_exc())
            ai_response = f"⚠️ AI bilan bog'lanishda xatolik: {str(e)}"
    
    # Save AI response
    ChatMessage.objects.create(
        session=session,
        role='assistant',
        content=ai_response,
    )
    
    return redirect('ai_yordamchi:chat_session', pk=pk)


@login_required
@require_POST
def clear_history(request, pk):
    """Clear chat history for a session."""
    session = get_object_or_404(ChatSession, pk=pk, user=request.user)
    session.messages.all().delete()
    session.title = "Yangi suhbat"
    session.save()
    messages.success(request, "Suhbat tarixi tozalandi.")
    return redirect('ai_yordamchi:chat_session', pk=pk)


@login_required
@require_POST
def delete_session(request, pk):
    """Delete a chat session."""
    session = get_object_or_404(ChatSession, pk=pk, user=request.user)
    session.delete()
    messages.success(request, "Suhbat o'chirildi.")
    return redirect('ai_yordamchi:chat_home')


def admin_required(view_func):
    """Decorator to require admin role."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_admin:
            messages.error(request, "Bu sahifaga faqat adminlar kira oladi.")
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return wrapper


@login_required
@admin_required
def admin_chats(request):
    """Admin view to see all chats."""
    sessions = ChatSession.objects.select_related('user').all()
    
    # Filter by user
    user_id = request.GET.get('user')
    if user_id:
        sessions = sessions.filter(user_id=user_id)
    
    paginator = Paginator(sessions, 20)
    page = request.GET.get('page')
    sessions = paginator.get_page(page)
    
    return render(request, 'ai_yordamchi/admin/chats.html', {
        'sessions': sessions,
    })


@login_required
@admin_required
def admin_chat_detail(request, pk):
    """View specific chat messages."""
    session = get_object_or_404(ChatSession, pk=pk)
    chat_messages = session.messages.all()
    
    return render(request, 'ai_yordamchi/admin/chat_detail.html', {
        'session': session,
        'chat_messages': chat_messages,
    })


def get_client_ip(request):
    """Get client IP address from request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip
