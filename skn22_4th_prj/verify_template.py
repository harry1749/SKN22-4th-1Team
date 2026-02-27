import os
import django
from django.template import loader, Context
from django.conf import settings

# Minimal Django setup for template testing
if not settings.configured:
    settings.configure(
        DEBUG=True,
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [os.path.join(os.getcwd(), "templates")],
            }
        ],
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth"],
    )
    django.setup()


def test_template_render():
    template_name = "symptom_result.html"
    mock_context = {
        "symptom": "머리 아파",
        "answer": "테스트 답변입니다.",
        "ingredients_data": [
            {
                "name": "Acetaminophen",
                "can_take": True,
                "reason": "안전합니다.",
                "dur_warning_types": ["임부 금기"],
                "products": [
                    {
                        "brand_name": "Tylenol",
                        "purpose": "Pain relief",
                        "active_ingredient": "Acetaminophen",
                    }
                ],
                "kr_durs": [{"type": "금기", "warning": "주의하세요"}],
                "fda_warning": "None",
            }
        ],
        "maps_key": "mock_key",
    }

    try:
        t = loader.get_template(template_name)
        rendered = t.render(mock_context)
        print("SUCCESS: Template rendered without SyntaxError.")
        # print(rendered[:200]) # Peek at the beginning
    except Exception as e:
        import traceback

        print(f"FAILURE: Template rendering failed:\n{traceback.format_exc()}")


if __name__ == "__main__":
    test_template_render()
