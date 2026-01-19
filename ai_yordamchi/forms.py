from django import forms

class ChatForm(forms.Form):
    text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Xabar yoz..."
        })
    )
    file = forms.FileField(required=False)
