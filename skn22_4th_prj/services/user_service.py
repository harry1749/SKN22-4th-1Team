class UserService:
    @staticmethod
    def _to_profile_namespace(data: dict):
        """프로필 데이터를 템플릿 친화적인 namespace로 변환."""
        from types import SimpleNamespace

        defaults = {
            "current_medications": "",
            "allergies": "",
            "chronic_diseases": "",
            "applied_allergies": "",
            "applied_chronic_diseases": "",
            "food_allergy_detail": "",
            "is_pregnant": False,
        }

        if not data:
            return SimpleNamespace(**defaults)
        return SimpleNamespace(**{**defaults, **data})

    @staticmethod
    async def get_profile(user_info: dict):
        """Supabase에서 사용자 프로필 조회."""
        if not user_info or "id" not in user_info:
            return UserService._to_profile_namespace(None)

        from services.supabase_service import SupabaseService

        profile_data = await SupabaseService.get_user_profile(user_info["id"])
        return UserService._to_profile_namespace(profile_data)

    @staticmethod
    async def update_profile(
        user_info: dict,
        current_medications: str,
        allergies: str,
        chronic_diseases: str,
        is_pregnant: bool = False,
        main_ingr_eng: str = "",
        applied_allergies: str = "",
        applied_chronic_diseases: str = "",
        food_allergy_detail: str = "",
    ):
        """Supabase 사용자 프로필 업데이트."""
        if not user_info or "id" not in user_info:
            return None

        from services.supabase_service import SupabaseService

        profile_data = await SupabaseService.update_user_profile(
            user_info["id"],
            current_medications,
            allergies,
            chronic_diseases,
            is_pregnant,
            main_ingr_eng,
            applied_allergies,
            applied_chronic_diseases,
            food_allergy_detail,
        )
        return UserService._to_profile_namespace(profile_data) if profile_data else None

    @staticmethod
    async def delete_account(user_info: dict):
        """사용자 계정 및 프로필 삭제."""
        if not user_info or "id" not in user_info:
            return False, "사용자 정보가 없습니다."

        from services.supabase_service import SupabaseService

        await SupabaseService.delete_user_profile(user_info["id"])
        auth_deleted, error = await SupabaseService.auth_delete_user(user_info["id"])

        if auth_deleted:
            return True, None
        return False, error
