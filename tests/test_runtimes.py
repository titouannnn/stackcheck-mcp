import json
import random
from datetime import date

import httpx2
import pytest

from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.runtimes import (
    Product,
    fetch_product,
    lookup_product,
    reduce_product,
    resolve_target,
    resolve_targets,
    runtime_info,
    runtime_infos,
    suggest_slugs,
)
from tests.conftest import TODAY, mock_http

TOMORROW = date(2026, 10, 28)


async def product(fetcher: Fetcher, slug: str) -> Product:
    return await fetch_product(fetcher, slug)


def test_reduce_product_handles_null_latest_and_dates() -> None:
    raw = {
        "result": {
            "name": "demo",
            "releases": [
                {
                    "name": "2",
                    "releaseDate": "2026-01-01",
                    "isLts": False,
                    "ltsFrom": None,
                    "isMaintained": True,
                    "eolFrom": None,
                    "latest": None,
                },
                {
                    "name": "1",
                    "releaseDate": "2020-01-01",
                    "isLts": True,
                    "ltsFrom": None,
                    "isMaintained": False,
                    "eolFrom": "2025-01-01",
                    "latest": {"name": "1.9.3", "date": "2024-01-01", "link": None},
                },
            ],
        }
    }
    reduced = reduce_product(raw)
    assert reduced["releases"][0] == {
        "cycle": "2",
        "latest": "2",
        "released": "2026-01-01",
        "maintained": True,
    }
    assert reduced["releases"][1] == {
        "cycle": "1",
        "latest": "1.9.3",
        "released": "2020-01-01",
        "lts": True,
        "eol": "2025-01-01",
    }


async def test_node_lts_switches_on_2026_10_28(fetcher: Fetcher) -> None:
    node = await product(fetcher, "nodejs")
    today = runtime_info(node, TODAY)
    assert today["lts"] == "24.22.1"
    assert today["latest"] == "26.12.1"
    assert today["next_lts"] == {"version": "26", "from": "2026-10-28"}
    assert today["eol"] == "2028-04-30"
    # 20 and 18 still have isMaintained (paid extended support) but are past EOL.
    assert today["maintained"] == ["26", "24", "22"]

    tomorrow = runtime_info(node, TOMORROW)
    assert tomorrow["lts"] == "26.12.1"
    assert "next_lts" not in tomorrow
    assert tomorrow["eol"] == "2029-04-30"

    assert resolve_target("node", node, None, TODAY) == "24.22.1"
    assert resolve_target("node", node, None, TOMORROW) == "26.12.1"


async def test_release_order_is_recomputed(fetcher: Fetcher) -> None:
    node = await product(fetcher, "nodejs")
    shuffled: Product = {"name": "nodejs", "releases": list(node["releases"])}
    random.Random(4).shuffle(shuffled["releases"])
    assert runtime_info(shuffled, TODAY) == runtime_info(node, TODAY)


async def test_python_default_is_newest_released_maintained_cycle(fetcher: Fetcher) -> None:
    python = await product(fetcher, "python")
    assert resolve_target("python", python, None, TODAY) == "3.15.2"
    # 3.15.0 was released on 2026-10-09: before that, the default is 3.14.
    assert resolve_target("python", python, None, date(2026, 10, 8)) == "3.14.10"
    info = runtime_info(python, TODAY)
    assert "lts" not in info and "next_lts" not in info
    assert info["latest"] == "3.15.2"
    assert info["maintained"][0] == "3.15"
    assert len(info["maintained"]) <= 5


async def test_target_specs(fetcher: Fetcher) -> None:
    python = await product(fetcher, "python")
    node = await product(fetcher, "nodejs")
    assert resolve_target("python", python, "3.14", TODAY) == "3.14.10"
    assert resolve_target("python", python, "3", TODAY) == "3.15.2"
    assert resolve_target("python", python, "3.14.2", TODAY) == "3.14.2"
    assert resolve_target("node", node, "v22", TODAY) == "22.23.4"
    assert resolve_target("node", node, "18", TODAY).startswith("18.")
    with pytest.raises(ValueError, match="unknown node version"):
        resolve_target("node", node, "99", TODAY)


async def test_java_default_is_temurin_lts_without_lts_from(fetcher: Fetcher) -> None:
    temurin = await product(fetcher, "eclipse-temurin")
    assert resolve_target("java", temurin, None, TODAY) == "25.0.5+8"
    assert resolve_target("java", temurin, "21", TODAY).startswith("21.")
    assert runtime_info(temurin, TODAY)["lts"] == "25.0.5+8"


async def test_product_without_lts(fetcher: Fetcher) -> None:
    info = runtime_info(await product(fetcher, "sqlite"), TODAY)
    assert info == {"latest": "3.54.1", "maintained": ["3"]}


def test_end_of_life_lts_is_flagged_and_not_recommended() -> None:
    # Laravel's shape on endoflife.date: its newest LTS (6) is EOL since 2022.
    laravel: Product = {
        "name": "laravel",
        "releases": [
            {"cycle": "13", "latest": "13.38.0", "released": "2026-03-17", "maintained": True,
             "eol": "2028-03-17"},
            {"cycle": "11", "latest": "11.57.0", "released": "2024-03-12", "eol": "2026-03-12"},
            {"cycle": "6", "latest": "6.20.45", "released": "2019-09-03", "lts": True,
             "eol": "2022-09-06"},
        ],
    }  # fmt: skip
    assert runtime_info(laravel, TODAY) == {
        "latest": "13.38.0",
        "lts": "6.20.45",
        "lts_eol": True,
        "maintained": ["13"],
        "eol": "2028-03-17",
    }
    # A dead LTS is never a default target either: the newest maintained cycle is.
    assert resolve_target("java", laravel, None, TODAY) == "13.38.0"


async def test_runtime_infos_partial_failure(fetcher: Fetcher) -> None:
    infos, errors = await runtime_infos(fetcher, ["nodejs", "python", "no such!"], TODAY)
    assert set(infos) == {"nodejs", "python"}
    assert errors == {"no such!": "invalid product slug"}


async def test_resolve_targets(fetcher: Fetcher) -> None:
    resolved, errors = await resolve_targets(
        fetcher, {"node": None, "python": "3.14", "java": "1"}, TODAY
    )
    assert resolved == {"node": "24.22.1", "python": "3.14.10"}
    assert "unknown java version" in errors["java"]


def test_suggest_slugs() -> None:
    products = [
        {"name": "nodejs", "aliases": ["node"]},
        {"name": "python", "aliases": []},
        {"name": "eclipse-temurin", "aliases": ["temurin"]},
        {"name": "oracle-jdk", "aliases": []},
        {"name": "pypy", "aliases": []},
    ]
    assert suggest_slugs("pyton", products)[0] == "python"
    assert suggest_slugs("nodej", products)[0] == "nodejs"
    assert suggest_slugs("temurin-jdk", products)[0] == "eclipse-temurin"
    assert len(suggest_slugs("p", products)) <= 3
    assert suggest_slugs("zzzzzzzzzzzz", products) == []


async def test_unknown_slug_suggests_from_product_list(tmp_path: object) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v1/products":
            body = {
                "result": [
                    {"name": "nodejs", "aliases": ["node"]},
                    {"name": "python", "aliases": []},
                ]
            }
            return httpx2.Response(200, content=json.dumps(body))
        return httpx2.Response(404, text="<html>Page not Found</html>")

    http = mock_http(handler)
    fetcher = Fetcher(http, Cache(None))
    with pytest.raises(ValueError, match="did you mean: python"):
        await lookup_product(fetcher, "pythn")
    await http.aclose()


async def test_maintained_excludes_extended_support_after_eol(fetcher: Fetcher) -> None:
    spring = runtime_info(await product(fetcher, "spring-boot"), TODAY)
    assert spring["maintained"] == ["4.1", "4.0"]  # 3.5 is EOL since 2026-06-30
    assert spring["eol"] == "2027-07-31"
    node = await product(fetcher, "nodejs")
    assert runtime_info(node, date(2027, 5, 1))["maintained"] == ["26", "24"]
