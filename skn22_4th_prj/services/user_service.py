class UserService:
    @staticmethod
    async def get_profile(user_info: dict):
        """Supabase에서 유저 프로필 조회"""
        if not user_info or "id" not in user_info:
            return None
        
        from services.supabase_service import SupabaseService
        profile_data = await SupabaseService.get_user_profile(user_info["id"])
        
        if not profile_data:
            return None
            
        from types import SimpleNamespace
        return SimpleNamespace(**profile_data)

    @staticmethod
    async def update_profile(user_info: dict, medications: str, allergies: str, diseases: str):
        """Supabase에 유저 프로필 저장"""
        if not user_info or "id" not in user_info:
            return None
            
        from services.supabase_service import SupabaseService
        profile_data = await SupabaseService.update_user_profile(
            user_info["id"], medications, allergies, diseases
        )
        
        if profile_data:
            from types import SimpleNamespace
            return SimpleNamespace(**profile_data)
        return None
