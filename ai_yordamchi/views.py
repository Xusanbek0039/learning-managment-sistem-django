from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.conf import settings
from openai import OpenAI
from django.utils import timezone

from .models import ChatSession, ChatMessage
from .forms import ChatForm


# OpenAI client (timeout bilan — loading bo‘lib qolmasligi uchun)
client = OpenAI(
    api_key=settings.OPENAI_API_KEY,
    timeout=20,  # sekund
)


@login_required
def ai_chat(request):
    """
    Foydalanuvchi uchun AI chat:
    - history saqlanadi
    - file upload mumkin
    - OpenAI error bo‘lsa ham sahifa ishlaydi
    """

    # 1. Chat session (har userga 1 ta chat — hozircha)
    chat, created = ChatSession.objects.get_or_create(user=request.user)

    # 2. Oldingi xabarlar
    messages = chat.messages.order_by("created_at")

    if request.method == "POST":
        form = ChatForm(request.POST, request.FILES)

        if form.is_valid():
            text = (form.cleaned_data.get("text") or "").strip()
            file = form.cleaned_data.get("file")

            # 3. USER MESSAGE saqlash
            if text or file:
                ChatMessage.objects.create(
                    chat=chat,
                    role="user",
                    text=text,
                    file=file,
                )

            # 4. AI uchun history tayyorlash
            history = []
            for m in messages:
                if m.text:
                    history.append({
                        "role": "user" if m.role == "user" else "assistant",
                        "content": m.text
                    })

            if text:
                history.append({"role": "user", "content": text})

                # 5. OpenAI so‘rovi (xatoga chidamli)
                try:
                    response = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=history,
                    )

                    ai_text = response.choices[0].message.content

                except Exception as e:
                    print("OPENAI ERROR:", e)
                    ai_text = "⚠️ AI bilan bog‘lanishda muammo bo‘ldi. Iltimos, keyinroq urinib ko‘ring."

                # 6. AI MESSAGE saqlash
                ChatMessage.objects.create(
                    chat=chat,
                    role="ai",
                    text=ai_text,
                )

        return redirect("ai_chat")

    else:
        form = ChatForm()

    return render(request, "chat.html", {
        "form": form,
        "messages": messages,
    })
