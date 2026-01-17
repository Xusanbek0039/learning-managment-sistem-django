from django import forms
from .models import Post, PostComment


class PostForm(forms.ModelForm):
    title = forms.CharField(
        max_length=300,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Post sarlavhasi'})
    )
    content = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Post matni...', 'rows': 6})
    )
    image = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={'class': 'form-control'})
    )
    post_type = forms.ChoiceField(
        choices=Post.POST_TYPES,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    is_published = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )

    class Meta:
        model = Post
        fields = ['title', 'content', 'image', 'post_type', 'is_published']


class CommentForm(forms.ModelForm):
    content = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'placeholder': 'Izoh yozing...',
            'rows': 3
        })
    )

    class Meta:
        model = PostComment
        fields = ['content']
