import httpx2
import pytest

from stackcheck_mcp.cache import Cache, Fetcher
from stackcheck_mcp.maven import (
    check_maven,
    interpolate,
    is_date_version,
    java_major,
    managed_versions,
    parse_spec,
    pick,
    reduce_metadata,
    reduce_pom,
)
from tests.conftest import mock_http
from tests.reference_stack import MAVEN, MAVEN_BOM

JAVA_25 = "25.0.5+8"
WEB = "org.springframework.boot:spring-boot-starter-web"
DATABIND = "com.fasterxml.jackson.core:jackson-databind"
NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def metadata(*versions: str, release: str | None = None) -> str:
    items = "".join(f"<version>{v}</version>" for v in versions)
    tag = f"<release>{release}</release>" if release else ""
    return f"<metadata><versioning>{tag}<versions>{items}</versions></versioning></metadata>"


def pom(
    coords: str,
    *,
    parent: str | None = None,
    props: dict[str, str] | None = None,
    managed: dict[str, str] | None = None,
    imports: list[str] | None = None,
) -> str:
    group, artifact, version = coords.split(":")
    parts = [f"<project {NS}><modelVersion>4.0.0</modelVersion>"]
    if parent:
        pg, pa, pv = parent.split(":")
        parts.append(
            f"<parent><groupId>{pg}</groupId><artifactId>{pa}</artifactId>"
            f"<version>{pv}</version></parent>"
        )
    parts.append(
        f"<groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>"
    )
    if props:
        parts.append(
            "<properties>" + "".join(f"<{k}>{v}</{k}>" for k, v in props.items()) + "</properties>"
        )
    deps = []
    for ga, v in (managed or {}).items():
        g, a = ga.split(":")
        deps.append(
            f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
            f"<version>{v}</version></dependency>"
        )
    for item in imports or []:
        g, a, v = item.split(":")
        deps.append(
            f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
            f"<version>{v}</version><type>pom</type><scope>import</scope></dependency>"
        )
    if deps:
        parts.append(
            "<dependencyManagement><dependencies>"
            + "".join(deps)
            + "</dependencies></dependencyManagement>"
        )
    parts.append("</project>")
    return "".join(parts)


def central(files: dict[str, str]) -> Fetcher:
    """A fake Maven Central keyed by ``g:a:v`` (POMs) and ``g:a`` (metadata)."""
    urls = {}
    for key, body in files.items():
        parts = key.split(":")
        base = f"/maven2/{parts[0].replace('.', '/')}/{parts[1]}"
        if len(parts) == 3:
            urls[f"{base}/{parts[2]}/{parts[1]}-{parts[2]}.pom"] = body
        else:
            urls[f"{base}/maven-metadata.xml"] = body

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = urls.get(request.url.path)
        return httpx2.Response(200, text=body) if body else httpx2.Response(404)

    return Fetcher(mock_http(handler), Cache(None))


# ----------------------------------------------------------------- recorded data


async def test_reference_maven_stack(fetcher: Fetcher) -> None:
    result = await check_maven(fetcher, MAVEN, MAVEN_BOM, JAVA_25)
    assert result.errors == {}
    assert result.picks == {
        WEB: "4.1.2",  # <release> says 4.2.0-RC1
        DATABIND: "2.22.4",
        MAVEN_BOM: "4.1.2",
    }
    assert result.issues == [
        {
            "kind": "bom",
            "ecosystem": "maven",
            "package": f"{DATABIND}:2.22.4",
            "required_by": f"{MAVEN_BOM}:4.1.2",
            "suggestion": {DATABIND: "2.21.7"},  # via the imported jackson-bom
            "severity": "warn",
        }
    ]


async def test_spring_boot_java_baseline(fetcher: Fetcher) -> None:
    result = await check_maven(fetcher, MAVEN, MAVEN_BOM, "11.0.29+7")
    java = [i for i in result.issues if i["kind"] == "java"]
    assert java == [
        {
            "kind": "java",
            "ecosystem": "maven",
            "package": f"{WEB}:4.1.2",
            "range": ">=17",  # java.version of spring-boot-starter-parent 4.1.2
            "target": "java 11.0.29+7",
            "suggestion": {WEB: "2.7.18"},
        }
    ]


async def test_user_ranges(fetcher: Fetcher) -> None:
    result = await check_maven(fetcher, [f"{WEB}:[4.0,4.1)"], None, None)
    assert result.picks == {WEB: "4.0.9"}
    result = await check_maven(fetcher, [f"{WEB}:4.1.2", f"{DATABIND}:[99,)"], None, None)
    assert result.picks == {WEB: "4.1.2"}
    assert result.issues[0]["kind"] == "unsatisfiable"
    assert result.issues[0]["message"] == "no stable version matches; latest is 2.22.4"


# ----------------------------------------------------------------- pure rules


def test_metadata_filters_prereleases_and_ignores_release_tag() -> None:
    xml = metadata(
        "1.0.0.RELEASE",
        "4.1.2",
        "4.2.0-M1",
        "4.2.0-RC1",
        "4.1.10",
        "4.1.9",
        "5.0.0-SNAPSHOT",
        "4.1.3.CR1",
        "4.1.11-beta-2",
        release="4.2.0-RC1",
    )
    assert reduce_metadata(xml) == {"versions": ["1.0.0.RELEASE", "4.1.2", "4.1.9", "4.1.10"]}


def test_build_date_versions_are_not_picked_as_latest() -> None:
    # commons-collections: Maven orders 20040616 above 3.2.2; 3.2.2 is the real latest.
    meta = reduce_metadata(metadata("1.0", "3.1", "20031027.000000", "3.2.2", "20040616"))
    assert pick(meta) == "3.2.2"
    assert pick(meta, parse_spec("commons-collections:commons-collections:[3.0,3.2)")) == "3.1"
    # Asked for explicitly, or nothing else exists: still a valid answer.
    assert pick(meta, parse_spec("commons-collections:commons-collections:20040616")) == (
        "20040616"
    )
    assert pick(reduce_metadata(metadata("20040616"))) == "20040616"
    assert not is_date_version("2026.0.2") and not is_date_version("4.2.19.Final")


async def test_unsatisfiable_message_names_the_real_latest() -> None:
    fake = central({"commons-collections:commons-collections": metadata("3.2.2", "20040616")})
    result = await check_maven(fake, ["commons-collections:commons-collections:[4,)"], None, None)
    assert result.issues[0]["message"] == "no stable version matches; latest is 3.2.2"
    result = await check_maven(fake, ["commons-collections:commons-collections"], None, None)
    assert result.picks == {"commons-collections:commons-collections": "3.2.2"}


def test_parse_spec() -> None:
    spec = parse_spec(f"{WEB}:[3.5,4.0)")
    assert spec.ga == WEB and spec.range == "[3.5,4.0)"
    assert parse_spec(WEB).restrictions == ()
    for bad in ["no-colon", "g:", ":a", "../x:y", "g:a:[1,", "g/x:a", "g:a b"]:
        with pytest.raises(ValueError):
            parse_spec(bad)


def test_reduce_pom_namespaces_and_inherited_coordinates() -> None:
    reduced = reduce_pom(
        pom(
            "g:child:1.0",
            parent="g:parent:7",
            props={"a": "1"},
            managed={"x:y": "${a}"},
            imports=["x:bom:2"],
        )
    )
    assert reduced == {
        "g": "g",
        "a": "child",
        "v": "1.0",
        "parent": "g:parent:7",
        "props": {"a": "1"},
        "managed": {"x:y": "${a}"},
        "imports": ["x:bom:2"],
    }
    inherited = reduce_pom(
        f"<project {NS}><parent><groupId>p</groupId><artifactId>pa</artifactId>"
        "<version>3</version></parent><artifactId>c</artifactId></project>"
    )
    assert (inherited["g"], inherited["v"]) == ("p", "3")


def test_interpolate() -> None:
    props = {"jackson.version": "2.21.7", "jackson.version.databind": "${jackson.version}"}
    assert interpolate("${jackson.version.databind}", props) == "2.21.7"
    assert interpolate("${missing}", props) == "${missing}"
    assert interpolate("${a}", {"a": "${a}"}) == "${a}"  # self-reference terminates


def test_java_major() -> None:
    assert java_major("17") == 17
    assert java_major("1.8") == 8
    assert java_major("21.0.5+11") == 21
    assert java_major("25-ea") == 25
    assert java_major("1") == 1
    assert java_major("abc") is None


# ----------------------------------------------------------------- BOM resolution


async def test_bom_properties_parents_and_imports() -> None:
    fetcher = central(
        {
            "acme:bom:1": pom(
                "acme:bom:1",
                props={"lib.version": "1.2", "b.version": "3"},
                managed={"acme:lib": "${lib.version}", "acme:dup": "1.0"},
                imports=["acme:bom-b:${b.version}", "acme:bom-c:1"],
            ),
            "acme:bom-b:3": pom(
                "acme:bom-b:3",
                parent="acme:parent:9",
                managed={
                    "acme:other": "${other.version}",
                    "acme:dup": "2.0",
                    "acme:self": "${project.version}",
                },
                imports=["acme:bom:1"],
            ),  # cycle back to the root
            "acme:parent:9": pom("acme:parent:9", props={"other.version": "4.5"}),
            "acme:bom-c:1": pom("acme:bom-c:1", managed={"acme:only-c": "7", "acme:other": "0.1"}),
        }
    )
    found = await managed_versions(
        fetcher,
        "acme:bom:1",
        {"acme:lib", "acme:dup", "acme:other", "acme:only-c", "acme:self", "acme:absent"},
    )
    assert found == {
        "acme:lib": "1.2",  # own property
        "acme:dup": "1.0",  # direct declaration beats imports
        "acme:other": "4.5",  # first import wins; property from bom-b's parent
        "acme:only-c": "7",
        "acme:self": "3",  # ${project.version} of the imported BOM
    }


async def test_bom_import_depth_is_limited() -> None:
    files = {
        f"acme:bom{i}:1": pom(f"acme:bom{i}:1", imports=[f"acme:bom{i + 1}:1"]) for i in range(7)
    }
    files["acme:bom7:1"] = pom("acme:bom7:1", managed={"acme:deep": "1"})
    files["acme:bom5:1"] = pom("acme:bom5:1", managed={"acme:depth5": "1"}, imports=["acme:bom6:1"])
    found = await managed_versions(central(files), "acme:bom0:1", {"acme:deep", "acme:depth5"})
    assert found == {"acme:depth5": "1"}  # bom7 sits 7 imports deep, past the limit of 5


async def test_partial_failures_and_bom_versions() -> None:
    fetcher = central(
        {
            "acme:lib": metadata("1.0", "2.0", "3.0-RC1"),
            "acme:bom": metadata("1"),
            "acme:bom:1": pom("acme:bom:1", managed={"acme:lib": "1.0"}, imports=["acme:gone:1"]),
        }
    )
    result = await check_maven(fetcher, ["acme:lib", "acme:missing", "bad"], "acme:bom", None)
    assert result.picks == {"acme:lib": "2.0", "acme:bom": "1"}
    assert result.errors == {
        "acme:missing": "not found",
        "bad": "expected groupId:artifactId[:version or range]",
    }
    assert [i["kind"] for i in result.issues] == ["bom"]
    assert result.issues[0]["suggestion"] == {"acme:lib": "1.0"}
