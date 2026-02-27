import os
import django
import asyncio

# Django 환경 설정
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skn22_4th_prj.settings")
django.setup()


async def test_search():
    try:
        from graph_agent.builder_v2 import build_graph

        print("1. build_graph import successful")

        inputs = {"query": "머리 아파", "user_profile": None}
        graph = build_graph()
        print("2. Graph built successful")

        print("3. Invoking graph (this may take time)...")
        # 실제 API 호출은 제외하고 그래프 구성까지만 먼저 확인하거나
        # API 키가 있다면 실행 시도
        # result = await graph.ainvoke(inputs)
        # print(f"Result category: {result.get('category')}")

    except ImportError as e:
        print(f"Import Error: {e}")
    except Exception as e:
        import traceback

        print(f"Error during execution:\n{traceback.format_exc()}")


if __name__ == "__main__":
    asyncio.run(test_search())
