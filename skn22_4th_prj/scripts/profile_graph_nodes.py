import argparse
import asyncio
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import django


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skn22_4th_prj.settings")
django.setup()


def _print_summary(query: str, result: dict, total_elapsed: float, timings: dict):
    summary = []
    for fn_name, values in timings.items():
        node_total = sum(values)
        node_avg = node_total / len(values)
        summary.append((fn_name, node_total, node_avg, len(values)))

    summary.sort(key=lambda x: x[1], reverse=True)

    print(f"query={query}")
    if isinstance(result, dict):
        print(f"category={result.get('category')}")
    print(f"total_elapsed_sec={total_elapsed:.3f}")
    print("node_timings_sec:")
    for fn_name, node_total, node_avg, count in summary:
        print(
            f"- {fn_name}: total={node_total:.3f}, avg={node_avg:.3f}, calls={count}"
        )

    if summary:
        slowest = summary[0]
        print(
            f"slowest_node={slowest[0]} total={slowest[1]:.3f}s avg={slowest[2]:.3f}s"
        )


async def main(query: str, timeout_sec: float):
    import graph_agent.builder_v2 as builder

    node_names = [
        "classify_node",
        "retrieve_data_node",
        "retrieve_fda_products_node",
        "retrieve_dur_node",
        "generate_symptom_answer_node",
        "generate_product_answer_node",
        "generate_general_answer_node",
        "generate_error_node",
    ]

    timings = defaultdict(list)

    def wrap_node(fn_name, fn):
        async def wrapped(state):
            start = time.perf_counter()
            try:
                return await fn(state)
            finally:
                elapsed = time.perf_counter() - start
                timings[fn_name].append(elapsed)

        return wrapped

    for fn_name in node_names:
        original = getattr(builder, fn_name, None)
        if original is not None:
            setattr(builder, fn_name, wrap_node(fn_name, original))

    graph = builder.build_graph()
    result = {}
    start_total = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            graph.ainvoke({"query": query, "user_info": None}),
            timeout=timeout_sec,
        )
    except Exception as e:
        total_elapsed = time.perf_counter() - start_total
        print(f"profile_error={type(e).__name__}: {e}")
        _print_summary(query, result, total_elapsed, timings)
        return

    total_elapsed = time.perf_counter() - start_total
    _print_summary(query, result, total_elapsed, timings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Profile LangGraph node durations.")
    parser.add_argument("--query", default="머리 아파")
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    args = parser.parse_args()
    asyncio.run(main(args.query, args.timeout_sec))
