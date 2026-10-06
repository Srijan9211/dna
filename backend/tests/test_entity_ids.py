"""Entity ids are opaque: an int (ShotGrid) or a non-numeric string (AYON)."""

from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, TypeAdapter, ValidationError

from dna.models.entity import Version
from dna.models.ids import EntityId, normalize_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        (42, 42),
        ("42", 42),  # ShotGrid ids arriving as text (URL segments, JSON strings)
        (" 42 ", 42),
        (42.0, 42),
        ("dna_test:5fd4ea6ac19911f1877e42dd13c3d9cf", "dna_test:5fd4ea6ac19911f1877e42dd13c3d9cf"),
        ("srijan9211", "srijan9211"),  # an AYON user id; digits inside a word stay text
        ("1234567890123456", "1234567890123456"),  # 16 digits: too long to be a tracker int
        ("12345678901234567890123456789012", "12345678901234567890123456789012"),
    ],
)
def test_normalize_id(raw, expected):
    result = normalize_id(raw)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize("bad", [True, False, "", "   ", 4.5, None, [1], {"id": 1}])
def test_normalize_id_rejects_things_that_cannot_be_ids(bad):
    with pytest.raises(ValueError):
        normalize_id(bad)


def test_entities_accept_either_kind_of_id():
    assert Version(id=7).id == 7
    assert Version(id="demo:abc").id == "demo:abc"
    assert Version(id="7").id == 7
    assert Version(id="demo:abc").__to_dict__()["id"] == "demo:abc"


def test_serialises_without_change_and_publishes_an_integer_or_string_schema():
    adapter = TypeAdapter(EntityId)
    assert adapter.dump_python(5) == 5 and adapter.dump_python("a:b") == "a:b"
    assert adapter.json_schema() == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    with pytest.raises(ValidationError):
        adapter.validate_python(True)


# FastAPI 0.104 ignores ``Annotated[..., validator]`` on *path* parameters, which is
# why EntityId is a schema-bearing class.  These tests pin that it works everywhere.


def _app() -> TestClient:
    app = FastAPI()

    class Body(BaseModel):
        version_id: EntityId
        parent: Optional[EntityId] = None

    @app.get("/things/{thing_id}")
    def read(thing_id: EntityId, related: Optional[EntityId] = None):
        return {"id": thing_id, "type": type(thing_id).__name__, "related": related}

    @app.post("/things")
    def create(body: Body):
        return {"id": body.version_id, "type": type(body.version_id).__name__}

    return TestClient(app)


def test_path_parameter_keeps_integers_as_integers():
    client = _app()
    assert client.get("/things/123").json() == {"id": 123, "type": "int", "related": None}


def test_path_parameter_keeps_ayon_ids_as_strings():
    client = _app()
    response = client.get("/things/dna_test:5fd4ea6ac19911f1877e42dd13c3d9cf?related=9")
    assert response.json() == {
        "id": "dna_test:5fd4ea6ac19911f1877e42dd13c3d9cf",
        "type": "str",
        "related": 9,
    }


def test_body_accepts_numbers_and_strings():
    client = _app()
    assert client.post("/things", json={"version_id": 42}).json() == {"id": 42, "type": "int"}
    assert client.post("/things", json={"version_id": "42"}).json() == {"id": 42, "type": "int"}
    assert client.post("/things", json={"version_id": "demo:abc"}).json() == {"id": "demo:abc", "type": "str"}
    assert client.post("/things", json={"version_id": True}).status_code == 422
    assert client.post("/things", json={"version_id": ""}).status_code == 422


def test_openapi_documents_ids_as_integer_or_string():
    schema = _app().get("/openapi.json").json()
    param = schema["paths"]["/things/{thing_id}"]["get"]["parameters"][0]["schema"]
    assert param["anyOf"] == [{"type": "integer"}, {"type": "string"}]


def test_the_real_api_routes_take_ayon_ids():
    """A playlist route accepts an AYON id end to end (mock provider, so a 404 for
    the unknown id proves it was parsed and routed, not rejected as a bad int)."""
    import main

    client = TestClient(main.app)
    response = client.get("/playlists/dna_test:5fd4ea6ac19911f1877e42dd13c3d9cf/versions")
    assert response.status_code != 422


def test_every_id_parameter_in_the_real_api_accepts_a_string_id():
    """No route may still declare an integer id: an AYON id would be rejected with 422.

    Walks the live OpenAPI schema, so a route added later with a plain ``int`` id
    fails here instead of in front of an AYON studio.
    """
    import main

    schema = main.app.openapi()
    offenders = []
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            for param in op.get("parameters", []):
                name = param["name"]
                is_id = name.endswith("_id") and name not in ("vexa_meeting_id", "segment_id", "attachment_id")
                if is_id and param["schema"].get("type") == "integer":
                    offenders.append(f"{method.upper()} {path} ({name})")
    assert not offenders, f"integer-only id parameters: {offenders}"
