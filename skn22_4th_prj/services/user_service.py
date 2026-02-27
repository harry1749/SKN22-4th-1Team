from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from drug.models import UserProfile


class UserService:
    @staticmethod
    @sync_to_async
    def get_profile(user: User):
        try:
            return user.profile
        except (UserProfile.DoesNotExist, AttributeError):
            return None

    @staticmethod
    @sync_to_async
    def update_profile(user: User, medications: str, allergies: str, diseases: str):
        profile, created = UserProfile.objects.get_or_create(user=user)
        profile.current_medications = medications
        profile.allergies = allergies
        profile.chronic_diseases = diseases
        profile.save()
        return profile
