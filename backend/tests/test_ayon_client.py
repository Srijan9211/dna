"""Transport behaviour of the AYON client: retries, timeouts and error mapping."""

from unittest.mock import patch

import pytest
import requests

from dna.prodtrack_providers.ayon_client import AyonClient
from dna.prodtrack_providers.prodtrack_provider_base import (
    ProdtrackAuthError,
    ProdtrackUnavailableError,
)


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = b"{}"
        self.text = ""

    def json(self):
        return self._body

    def close(self):
        pass


class FakeSession:
    """Plays back a script of responses or exceptions, recording each call."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("dna.prodtrack_providers.ayon_client.time.sleep"):
        yield


def client(*script, timeout=30.0):
    session = FakeSession(*script)
    return (
        AyonClient("http://ayon.test", "key", session=session, timeout=timeout),
        session,
    )


def test_a_read_is_retried_after_a_dropped_connection():
    c, s = client(requests.ConnectionError("reset"), FakeResponse(200, {"ok": 1}))
    assert c.rest("GET", "/api/info") == {"ok": 1}
    assert len(s.calls) == 2


def test_a_read_is_retried_on_gateway_errors_then_succeeds():
    c, s = client(FakeResponse(503), FakeResponse(502), FakeResponse(200, {"ok": 1}))
    assert c.rest("GET", "/api/info") == {"ok": 1}
    assert len(s.calls) == 3


def test_retries_are_bounded_and_end_in_a_domain_error():
    c, s = client(*[FakeResponse(503)] * 5)
    with pytest.raises(ProdtrackUnavailableError):
        c.rest("GET", "/api/info")
    assert len(s.calls) == 3  # one try and two retries


def test_a_write_is_never_replayed():
    c, s = client(requests.ReadTimeout("slow"), FakeResponse(200))
    with pytest.raises(ProdtrackUnavailableError):
        c.rest("POST", "/api/projects/p/activities", json={})
    assert len(s.calls) == 1


def test_graphql_queries_are_retried():
    c, s = client(FakeResponse(504), FakeResponse(200, {"data": {"x": 1}}))
    assert c.graphql("query { x }") == {"x": 1}
    assert len(s.calls) == 2


def test_authentication_failures_are_not_retried():
    c, s = client(FakeResponse(401))
    with pytest.raises(ProdtrackAuthError):
        c.rest("GET", "/api/users/me")
    assert len(s.calls) == 1


def test_connect_and_read_timeouts_are_separate():
    c, s = client(FakeResponse(200), timeout=30.0)
    c.rest("GET", "/api/info")
    assert s.calls[0][2]["timeout"] == (5.0, 30.0)
    c, s = client(FakeResponse(200), timeout=2.0)
    c.rest("GET", "/api/info")
    assert s.calls[0][2]["timeout"] == (2.0, 2.0)


def test_the_unreachable_message_does_not_leak_the_internal_address():
    c, _ = client(
        *[requests.ConnectionError("HTTPConnectionPool(host='10.1.2.3')")] * 3
    )
    with pytest.raises(ProdtrackUnavailableError) as err:
        c.rest("GET", "/api/info")
    assert "10.1.2.3" not in str(err.value)


def test_the_default_session_has_a_pool_sized_for_concurrent_requests():
    c = AyonClient("http://ayon.test", "key")
    assert c._session.get_adapter("http://ayon.test")._pool_maxsize == 50


def test_graphql_all_follows_every_page():
    def page(nodes, nxt):
        return FakeResponse(
            200,
            {
                "data": {
                    "users": {
                        "edges": [{"node": n} for n in nodes],
                        "pageInfo": {"hasNextPage": bool(nxt), "endCursor": nxt},
                    }
                }
            },
        )

    c, s = client(page([{"n": 1}, {"n": 2}], "c1"), page([{"n": 3}], None))
    got = c.graphql_all("query($first: Int, $after: String) { users }", ("users",))
    assert [n["n"] for n in got] == [1, 2, 3]
    assert s.calls[1][2]["json"]["variables"]["after"] == "c1"
