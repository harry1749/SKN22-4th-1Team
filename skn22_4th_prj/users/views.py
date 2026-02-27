from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.models import User
from django.contrib import messages
from services.user_service import UserService


def register_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        if User.objects.filter(username=username).exists():
            return render(
                request, "register.html", {"error": "이미 존재하는 사용자명입니다."}
            )
        user = User.objects.create_user(username=username, password=password)
        login(request, user)
        return redirect("chat:home")
    return render(request, "register.html")


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("chat:home")
        else:
            return render(
                request,
                "login.html",
                {"error": "아이디 또는 비밀번호가 올바르지 않습니다."},
            )
    return render(request, "login.html")


def logout_view(request):
    logout(request)
    return redirect("chat:home")


async def profile_view(request):
    if not request.user.is_authenticated:
        return redirect("users:login")

    profile = await UserService.get_profile(request.user)

    if request.method == "POST":
        medications = request.POST.get("medications", "")
        allergies = request.POST.get("allergies", "")
        diseases = request.POST.get("diseases", "")
        await UserService.update_profile(request.user, medications, allergies, diseases)
        return redirect("users:profile")

    return render(request, "profile.html", {"user": request.user, "profile": profile})
