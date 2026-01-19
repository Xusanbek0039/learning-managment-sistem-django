from django.shortcuts import render
from django.conf import settings
from openai import OpenAI

client = OpenAI(api_key=settings.OPENAI_API_KEY)

def ai_chat(request):
    answer = None
    question = ""

    if request.method == "POST":
        question = request.POST.get("question")

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Sen foydalanuvchiga yordam beradigan IT yordamchisan."},
                {"role": "user", "content": question},
            ]
        )

        answer = response.choices[0].message.content

    return render(request, "ai_yordamchi/chat.html", {
        "answer": answer,
        "question": question
    })
