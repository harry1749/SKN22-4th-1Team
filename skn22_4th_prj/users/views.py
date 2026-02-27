from django.shortcuts import render, redirect
from services.supabase_service import SupabaseService
from services.user_service import UserService
import asyncio


def register_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        
        # Supabase를 통한 회원가입 (동기식 호출을 위한 래핑이 필요할 수 있으나 여기선 간단히 작성)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        user = loop.run_until_complete(SupabaseService.auth_sign_up(username, password))
        
        if user:
            return redirect("users:login")
        else:
            return render(
                request, "register.html", {"error": "회원가입에 실패했습니다. (이미 존재하거나 비밀번호가 너무 짧을 수 있습니다.)"}
            )
    return render(request, "register.html")


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        user, session = loop.run_until_complete(SupabaseService.auth_sign_in(username, password))
        
        if user and session:
            # 세션에 사용자 정보 저장
            request.session["supabase_user"] = {
                "id": user.id,
                "email": user.email,
                "username": username
            }
            return redirect("chat:home")
        else:
            return render(
                request,
                "login.html",
                {"error": "아이디 또는 비밀번호가 올바르지 않습니다."},
            )
    return render(request, "login.html")


def logout_view(request):
    # 세션 정보 삭제
    if "supabase_user" in request.session:
        del request.session["supabase_user"]
    return redirect("chat:home")


async def profile_view(request):
    # 세션에서 사용자 정보 확인
    user_info = request.session.get("supabase_user")
    if not user_info:
        return redirect("users:login")

    profile = await UserService.get_profile(user_info)

    if request.method == "POST":
        medications = request.POST.get("medications", "")
        allergies = request.POST.get("allergies", "")
        diseases = request.POST.get("diseases", "")
        await UserService.update_profile(user_info, medications, allergies, diseases)
        return redirect("users:profile")

    return render(request, "profile.html", {"user": user_info, "profile": profile})
