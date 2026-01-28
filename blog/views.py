from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q
from .models import Post, PostComment, PostLike, CommentLike
from .forms import PostForm, CommentForm
from coin.models import ActivityLog

@login_required
def post_list(request):
    search_query = request.GET.get('q')
    filter_type = request.GET.get('type')
    
    posts = Post.objects.all()
    
    if search_query:
        posts = posts.filter(
            Q(title__icontains=search_query) | 
            Q(content__icontains=search_query)
        )
    
    if filter_type:
        posts = posts.filter(post_type=filter_type)
        
    # Random ordering per page load
    posts = posts.order_by('?')
    
    return render(request, 'blog/post_list.html', {
        'posts': posts, 
        'search_query': search_query, 
        'filter_type': filter_type
    })

@login_required
def post_detail(request, pk):
    post = get_object_or_404(Post, pk=pk)
    comments = post.comments.all()
    is_liked = post.likes.filter(user=request.user).exists()
    liked_comment_ids = list(CommentLike.objects.filter(
        user=request.user, 
        comment__in=comments
    ).values_list('comment_id', flat=True))
    
    if request.method == 'POST':
        form = CommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.post = post
            comment.user = request.user
            comment.save()
            
            # Award coin for comment
            request.user.add_coins(1, f"Izoh qoldirildi: {post.title}")
            ActivityLog.objects.create(
                user=request.user,
                action_type='Postga izoh qoldirdi',
                description=f"Postga izoh qoldirdi: {post.title}"
            )
            messages.success(request, "Izoh qoldirildi (+1 coin)!")
            return redirect('blog:post_detail', pk=pk)
    else:
        form = CommentForm()
        
    return render(request, 'blog/post_detail.html', {
        'post': post,
        'comments': comments,
        'is_liked': is_liked,
        'liked_comment_ids': liked_comment_ids,
        'form': form
    })

@login_required
def post_create(request):
    if not (request.user.is_teacher or request.user.is_admin):
        messages.error(request, "Faqat o'qituvchilar va adminlar post qo'shishi mumkin!")
        return redirect('blog:post_list')
        
    if request.method == 'POST':
        form = PostForm(request.POST, request.FILES)
        if form.is_valid():
            post = form.save(commit=False)
            post.author = request.user
            post.save()
            messages.success(request, "Post muvaffaqiyatli yaratildi!")
            return redirect('blog:post_list')
    else:
        form = PostForm()
        
    return render(request, 'blog/post_form.html', {'form': form})

@login_required
def toggle_like(request, pk):
    post = get_object_or_404(Post, pk=pk)
    like = PostLike.objects.filter(post=post, user=request.user).first()
    
    if like:
        # Unlike - coin qaytarib olish
        if like.coin_awarded:
            request.user.remove_coins(1, f"Postga like olib tashlandi: {post.title}")
            messages.info(request, "Like olib tashlandi (-1 coin)")
        like.delete()
    else:
        # Like - coin berish
        like = PostLike.objects.create(post=post, user=request.user, coin_awarded=True)
        request.user.add_coins(1, f"Postga like bosildi: {post.title}")
        ActivityLog.objects.create(
            user=request.user,
            action_type='like_post',
            description=f"Postga like bosdi: {post.title}"
        )
        messages.success(request, "Like bosildi (+1 coin)")
        
    return redirect('blog:post_detail', pk=pk)


@login_required
def admin_posts(request):
    if not (request.user.is_teacher or request.user.is_admin):
        messages.error(request, "Bu sahifaga kirish huquqingiz yo'q!")
        return redirect('blog:post_list')
    
    posts = Post.objects.all()
    
    if request.user.is_teacher and not request.user.is_admin:
        posts = posts.filter(author=request.user)
    
    post_type = request.GET.get('type')
    if post_type:
        posts = posts.filter(post_type=post_type)
    
    search = request.GET.get('q')
    if search:
        posts = posts.filter(Q(title__icontains=search) | Q(content__icontains=search))
    
    return render(request, 'blog/admin_posts.html', {
        'posts': posts.order_by('-created_at'),
        'post_types': Post.POST_TYPES,
    })


@login_required
def post_edit(request, pk):
    post = get_object_or_404(Post, pk=pk)
    
    if not request.user.is_admin and post.author != request.user:
        messages.error(request, "Bu postni tahrirlash huquqingiz yo'q!")
        return redirect('blog:post_list')
    
    if request.method == 'POST':
        form = PostForm(request.POST, request.FILES, instance=post)
        if form.is_valid():
            form.save()
            messages.success(request, "Post muvaffaqiyatli yangilandi!")
            return redirect('blog:admin_posts')
    else:
        form = PostForm(instance=post)
    
    return render(request, 'blog/post_form.html', {'form': form, 'post': post, 'edit_mode': True})


@login_required
def post_delete(request, pk):
    post = get_object_or_404(Post, pk=pk)
    
    if not request.user.is_admin and post.author != request.user:
        messages.error(request, "Bu postni o'chirish huquqingiz yo'q!")
        return redirect('blog:post_list')
    
    if request.method == 'POST':
        post.delete()
        messages.success(request, "Post o'chirildi!")
        return redirect('blog:admin_posts')
    
    return render(request, 'blog/post_delete.html', {'post': post})


@login_required
def toggle_comment_like(request, pk):
    comment = get_object_or_404(PostComment, pk=pk)
    like = CommentLike.objects.filter(comment=comment, user=request.user).first()
    
    if like:
        like.delete()
        comment.user.remove_coins(1, f"Izohga like olib tashlandi")
        messages.info(request, "Like olib tashlandi (-1 coin izoh egasidan)")
    else:
        CommentLike.objects.create(comment=comment, user=request.user)
        comment.user.add_coins(1, f"Izohga like olindi")
        ActivityLog.objects.create(
            user=comment.user,
            action_type='comment_liked',
            description=f"Izohga like olindi: {comment.content[:30]}"
        )
        messages.success(request, "Like bosildi (+1 coin izoh egasiga)")
    
    return redirect('blog:post_detail', pk=comment.post.pk)
