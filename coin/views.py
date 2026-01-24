from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from accounts.models import CustomUser
from .models import (
    Product, ProductLike, ProductComment, ProductPurchase, 
    CoinTransaction, ActivityLog
)


# ============== COIN MARKET ==============

@login_required
def market_list(request):
    products = Product.objects.filter(is_active=True)
    return render(request, 'coin/market/list.html', {'products': products})


@login_required
def market_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    comments = product.comments.all()
    is_liked = product.likes.filter(user=request.user).exists()
    can_purchase = request.user.coins >= product.coin_price and product.stock > 0
    
    if request.method == 'POST' and 'comment' in request.POST:
        content = request.POST.get('content')
        if content:
            ProductComment.objects.create(
                product=product,
                user=request.user,
                content=content
            )
            request.user.add_coins(1, f"Mahsulotga izoh: {product.name}")
            ActivityLog.objects.create(
                user=request.user,
                action_type='comment_product',
                description=f"Mahsulotga izoh qoldirdi: {product.name}"
            )
            messages.success(request, "Izoh qo'shildi (+1 coin)!")
            return redirect('coin:market_detail', pk=pk)
    
    return render(request, 'coin/market/detail.html', {
        'product': product,
        'comments': comments,
        'is_liked': is_liked,
        'can_purchase': can_purchase,
    })


@login_required
def market_like(request, pk):
    product = get_object_or_404(Product, pk=pk)
    like = ProductLike.objects.filter(product=product, user=request.user).first()
    
    if like:
        # Unlike - coin qaytarib olinmaydi
        like.delete()
    else:
        # Like - avval like bosgan bo'lsa coin berilmaydi
        previous_like = ProductLike.objects.filter(product=product, user=request.user, coin_awarded=True).exists()
        like = ProductLike.objects.create(product=product, user=request.user)
        
        if not previous_like:
            # Birinchi marta like - coin berish
            request.user.add_coins(1, f"Mahsulotga like: {product.name}")
            like.coin_awarded = True
            like.save()
            ActivityLog.objects.create(
                user=request.user,
                action_type='like_product',
                description=f"Mahsulotga like bosdi: {product.name}"
            )
    
    return redirect('coin:market_detail', pk=pk)


@login_required
def market_purchase(request, pk):
    product = get_object_or_404(Product, pk=pk)
    
    if request.user.coins < product.coin_price:
        messages.error(request, "Coinlaringiz yetarli emas!")
        return redirect('coin:market_detail', pk=pk)
    
    if product.stock <= 0:
        messages.error(request, "Mahsulot tugagan!")
        return redirect('coin:market_detail', pk=pk)
    
    if request.method == 'POST':
        request.user.coins -= product.coin_price
        request.user.save()
        
        product.stock -= 1
        product.save()
        
        ProductPurchase.objects.create(
            product=product,
            user=request.user,
            coins_spent=product.coin_price
        )
        
        CoinTransaction.objects.create(
            user=request.user,
            amount=product.coin_price,
            action='remove',
            reason=f"Mahsulot sotib olindi: {product.name}"
        )
        ActivityLog.objects.create(
            user=request.user,
            action_type='purchase_product',
            description=f"Mahsulot sotib oldi: {product.name} ({product.coin_price} coin)"
        )
        
        messages.success(request, f"'{product.name}' muvaffaqiyatli sotib olindi!")
        return redirect('coin:market_list')
    
    return render(request, 'coin/market/purchase_confirm.html', {'product': product})


# ============== ADMIN: COIN MARKET ==============

@login_required
def admin_products(request):
    if not request.user.is_admin:
        return redirect('home')
    
    products = Product.objects.all()
    return render(request, 'coin/admin/products.html', {'products': products})


@login_required
def admin_product_add(request):
    if not request.user.is_admin:
        return redirect('home')
    
    if request.method == 'POST':
        name = request.POST.get('name')
        description = request.POST.get('description')
        coin_price = request.POST.get('coin_price')
        stock = request.POST.get('stock', 0)
        image = request.FILES.get('image')
        
        if name and description and coin_price and image:
            Product.objects.create(
                name=name,
                description=description,
                coin_price=int(coin_price),
                stock=int(stock),
                image=image
            )
            messages.success(request, "Mahsulot qo'shildi!")
            return redirect('coin:admin_products')
        else:
            messages.error(request, "Barcha maydonlarni to'ldiring!")
    
    return render(request, 'coin/admin/product_form.html', {'title': "Mahsulot qo'shish"})


@login_required
def admin_product_edit(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    product = get_object_or_404(Product, pk=pk)
    
    if request.method == 'POST':
        product.name = request.POST.get('name')
        product.description = request.POST.get('description')
        product.coin_price = int(request.POST.get('coin_price'))
        product.stock = int(request.POST.get('stock', 0))
        product.is_active = 'is_active' in request.POST
        
        if request.FILES.get('image'):
            product.image = request.FILES.get('image')
        
        product.save()
        messages.success(request, "Mahsulot yangilandi!")
        return redirect('coin:admin_products')
    
    return render(request, 'coin/admin/product_form.html', {'title': "Mahsulotni tahrirlash", 'product': product})


@login_required
def admin_product_delete(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    product = get_object_or_404(Product, pk=pk)
    
    if request.method == 'POST':
        product.delete()
        messages.success(request, "Mahsulot o'chirildi!")
        return redirect('coin:admin_products')
    
    return render(request, 'coin/admin/product_delete.html', {'product': product})


@login_required
def admin_purchases(request):
    if not request.user.is_admin:
        return redirect('home')
    
    purchases = ProductPurchase.objects.all().select_related('product', 'user')
    return render(request, 'coin/admin/purchases.html', {'purchases': purchases})


@login_required
def admin_mark_delivered(request, pk):
    if not request.user.is_admin:
        return redirect('home')
    
    purchase = get_object_or_404(ProductPurchase, pk=pk)
    purchase.is_delivered = True
    purchase.save()
    messages.success(request, "Yetkazildi deb belgilandi!")
    return redirect('coin:admin_purchases')


# ============== ADMIN: ACTIVITY LOG ==============

@login_required
def admin_activities(request):
    if not request.user.is_admin:
        return redirect('home')
    
    activities = ActivityLog.objects.all().select_related('user')
    
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    action_type = request.GET.get('action_type')
    user_id = request.GET.get('user')
    
    if date_from:
        from datetime import datetime
        date_from = datetime.strptime(date_from, '%Y-%m-%d')
        activities = activities.filter(created_at__date__gte=date_from)
    
    if date_to:
        from datetime import datetime
        date_to = datetime.strptime(date_to, '%Y-%m-%d')
        activities = activities.filter(created_at__date__lte=date_to)
    
    if action_type:
        activities = activities.filter(action_type=action_type)
    
    if user_id:
        activities = activities.filter(user_id=user_id)
    
    users = CustomUser.objects.all()
    action_types = ActivityLog.ACTION_TYPES
    
    return render(request, 'coin/admin/activities.html', {
        'activities': activities[:500],
        'users': users,
        'action_types': action_types,
    })
