from django.shortcuts import render, redirect
from services.supabase_service import SupabaseService
from services.user_service import UserService
import asyncio


from django.http import JsonResponse

def register_view(request):
    if request.method == "POST":
        email = request.POST.get("email")
        password = request.POST.get("password")
        password_confirm = request.POST.get("password_confirm")
        
        if password != password_confirm:
            return JsonResponse({"status": "error", "message": "비밀번호가 서로 일치하지 않습니다."}, status=400)
        
        # 추가 건강 정보 추출
        current_medications = request.POST.get("current_medications", "")
        is_pregnant = request.POST.get("is_pregnant") == "on"
        
        # 기저질환 처리
        has_disease = request.POST.get("has_disease") == "on"
        chronic_diseases = request.POST.get("chronic_diseases", "") if has_disease else ""
        
        # 알레르기 처리
        has_allergy = request.POST.get("has_allergy") == "on"
        allergies = request.POST.get("allergies", "") if has_allergy else ""

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            user, error = loop.run_until_complete(SupabaseService.auth_sign_up(email, password))
            
            if user:
                # 회원가입 성공 시 프로필 생성
                loop.run_until_complete(SupabaseService.update_user_profile(
                    user.id, current_medications, allergies, chronic_diseases, is_pregnant
                ))
                return JsonResponse({"status": "success", "redirect": "/auth/login/"})
            else:
                if error == "exists":
                    return JsonResponse({"status": "error", "code": "user_exists", "message": "이미 존재하는 이메일입니다."}, status=400)
                return JsonResponse({"status": "error", "message": f"회원가입 실패: {error}"}, status=400)
        except Exception as e:
             return JsonResponse({"status": "error", "message": f"시스템 오류: {str(e)}"}, status=500)
        finally:
            loop.close()
            
    return render(request, "register.html")


def login_view(request):
    # 이미 로그인된 경우 메인으로 리다이렉트
    if "supabase_user" in request.session:
        return redirect("chat:home")

    if request.method == "POST":
        email = request.POST.get("email")
        password = request.POST.get("password")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        user, session = loop.run_until_complete(SupabaseService.auth_sign_in(email, password))
        
        if user and session:
            # 세션에 사용자 정보 저장
            request.session["supabase_user"] = {
                "id": user.id,
                "email": user.email,
                "display_name": email.split('@')[0]
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
    message = None
    error = None

    if request.method == "POST":
        action = request.POST.get("action")
        
        if action == "update_profile":
            current_medications = request.POST.get("current_medications", "")
            allergies = request.POST.get("allergies", "")
            chronic_diseases = request.POST.get("chronic_diseases", "")
            is_pregnant = request.POST.get("is_pregnant") == "on"
            
            updated_profile = await UserService.update_profile(user_info, current_medications, allergies, chronic_diseases, is_pregnant)
            if updated_profile:
                profile = updated_profile
                message = "건강 정보가 성공적으로 저장되었습니다."
            else:
                error = "정보 저장 중 오류가 발생했습니다."
            
        elif action == "update_password":
            new_password = request.POST.get("new_password")
            confirm_password = request.POST.get("confirm_password")
            
            if not new_password or new_password != confirm_password:
                error = "비밀번호가 일치하지 않거나 입력되지 않았습니다."
            else:
                user, auth_error = await SupabaseService.auth_update_password(new_password)
                if auth_error:
                    error = f"비밀번호 변경 실패: {auth_error}"
                else:
                    message = "비밀번호가 성공적으로 변경되었습니다."
                    
        elif action == "delete_account":
            success, delete_error = await UserService.delete_account(user_info)
            if success:
                # 세션 삭제 후 로그인 페이지로
                if "supabase_user" in request.session:
                    del request.session["supabase_user"]
                return redirect("users:login")
            else:
                error = f"회원 탈퇴 실패: {delete_error}"
    # 템플릿에 전달할 값을 Python에서 미리 준비 (|default 필터 사용하지 않음)
    no_info = "입력된 정보가 없습니다."
    
    meds_val = getattr(profile, 'current_medications', '') or ''
    allergy_val = getattr(profile, 'allergies', '') or ''
    disease_val = getattr(profile, 'chronic_diseases', '') or ''
    pregnant_val = getattr(profile, 'is_pregnant', False)

    return render(request, "profile.html", {
        "user": user_info, 
        "message": message,
        "error": error,
        "meds_display": meds_val if meds_val.strip() else no_info,
        "meds_value": meds_val,
        "allergy_display": allergy_val if allergy_val.strip() else no_info,
        "allergy_value": allergy_val,
        "disease_display": disease_val if disease_val.strip() else no_info,
        "disease_value": disease_val,
        "is_pregnant": pregnant_val,
        "pregnant_checked": "checked" if pregnant_val else "",
    })
