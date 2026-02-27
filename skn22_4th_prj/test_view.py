import os
import django
from django.test import RequestFactory
import asyncio

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skn22_4th_prj.settings")
django.setup()


async def test_view():
    from chat.views import smart_search

    factory = RequestFactory()
    request = factory.get("/smart-search/", {"q": "머리 아파"})

    # 흉내내기: user와 session 주입 (있어야 할 수도 있음)
    from django.contrib.auth.models import AnonymousUser

    request.user = AnonymousUser()

    try:
        response = await smart_search(request)
        print(f"Status Code: {response.status_code}")
        if hasattr(response, "content"):
            # 에러 페이지인 경우 내용 출력 (일부만)
            print("Content (first 500 chars):")
            print(response.content.decode("utf-8")[:500])
    except Exception as e:
        import traceback

        print(f"CRASH in View:\n{traceback.format_exc()}")


if __name__ == "__main__":
    asyncio.run(test_view())
