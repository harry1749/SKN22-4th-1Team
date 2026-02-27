from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chat"

    def ready(self):
        # Patch DrugService to use Supabase
        try:
            from services.drug_service import DrugService
            from services.supabase_service import SupabaseService
            import logging

            logger = logging.getLogger(__name__)
            logger.info("Patching DrugService with SupabaseService for DUR queries...")
            DrugService.get_dur_by_ingr = SupabaseService.get_dur_by_ingr
            DrugService.get_enriched_dur_info = SupabaseService.get_enriched_dur_info
        except Exception as e:
            print(f"Error during patching: {e}")
