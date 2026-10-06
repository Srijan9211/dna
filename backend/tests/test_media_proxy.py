"""Signed media URLs: the tokens, the proxy route, and the version-list integration."""

import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import main
from dna import configsys, media_proxy
from dna.media_proxy import MediaStream, sign_token, verify_token
from dna.models.entity import Version
from dna.prodtrack_providers.ayon_client import AyonNotFound

SECRET = "s" * 40


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in list(configsys.default_registry.known_env_names()):
        monkeypatch.delenv(name, raising=False)
    configsys.reset_config()
    yield
    main.app.dependency_overrides.clear()
    configsys.reset_config()


# ── tokens ───────────────────────────────────────────────────────────────────


def token(**kw):
    defaults = dict(version_id="demo:abc", kind="thumbnail", login="bob", secret=SECRET)
    return sign_token(**{**defaults, **kw})


def test_a_token_verifies_and_carries_the_user():
    claims = verify_token(token(), version_id="demo:abc", kind="thumbnail", secret=SECRET)
    assert claims["u"] == "bob" and claims["v"] == "demo:abc" and claims["k"] == "thumbnail"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),  # signature tampered
        lambda t: "x" + t,  # body tampered
        lambda t: t.split(".")[0],  # signature stripped
        lambda t: "",
        lambda t: "not-a-token",
        lambda t: "....",
    ],
)
def test_a_tampered_or_malformed_token_is_rejected_uniformly(mutate):
    with pytest.raises(ValueError, match="^Invalid media link.$"):
        verify_token(mutate(token()), version_id="demo:abc", kind="thumbnail", secret=SECRET)


def test_a_token_is_bound_to_its_version_and_kind():
    t = token()
    with pytest.raises(ValueError, match="Invalid media link"):
        verify_token(t, version_id="demo:OTHER", kind="thumbnail", secret=SECRET)
    with pytest.raises(ValueError, match="Invalid media link"):
        verify_token(t, version_id="demo:abc", kind="movie", secret=SECRET)


def test_a_token_expires():
    t = token(ttl=60, now=1000.0)
    verify_token(t, version_id="demo:abc", kind="thumbnail", secret=SECRET, now=1059.0)
    with pytest.raises(ValueError, match="expired"):
        verify_token(t, version_id="demo:abc", kind="thumbnail", secret=SECRET, now=1061.0)


def test_a_token_signed_with_another_secret_is_rejected():
    with pytest.raises(ValueError, match="Invalid media link"):
        verify_token(token(secret="a" * 40), version_id="demo:abc", kind="thumbnail", secret="b" * 40)


def test_without_a_jwt_secret_tokens_still_work_within_the_process():
    t = sign_token(version_id=7, kind="movie", login=None, secret=None)
    assert verify_token(t, version_id=7, kind="movie", secret=None)["u"] is None
    with pytest.raises(ValueError):  # ... but the ephemeral key is not a configured one
        verify_token(t, version_id=7, kind="movie", secret=SECRET)


def test_integer_ids_round_trip():
    claims = verify_token(token(version_id=1001), version_id=1001, kind="thumbnail", secret=SECRET)
    assert claims["v"] == 1001
    assert media_proxy.media_url(1001, "thumbnail", "T") == "/media/versions/1001/thumbnail?token=T"


def test_an_unknown_kind_cannot_be_signed():
    with pytest.raises(ValueError, match="Unknown media kind"):
        sign_token(version_id=1, kind="source", login=None, secret=SECRET)


# ── the route ────────────────────────────────────────────────────────────────


class MediaProvider:
    MEDIA_REQUIRES_PROXY = True

    def __init__(self, login=None):
        self.sudo_user = login
        self.opened = []

    def open_media(self, version_id, kind, range_header=None):
        self.opened.append((version_id, kind, range_header, self.sudo_user))
        closed = self.closed = mock.Mock()
        if kind == "movie" and version_id == "demo:none":
            raise AyonNotFound("no reviewable media")
        return MediaStream(
            206 if range_header else 200,
            {"Content-Type": "video/mp4", "Content-Range": "bytes 0-2/10", "X-Internal": "no"}
            if range_header
            else {"Content-Type": "image/jpeg"},
            iter([b"ab", b"c"]),
            closed,
        )


@pytest.fixture
def media(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    configsys.reset_config()
    made = []

    def factory(sudo_login=None, session_id=None):
        provider = MediaProvider(sudo_login)
        made.append(provider)
        return provider

    monkeypatch.setattr(main, "get_prodtrack_provider", factory)
    return TestClient(main.app), made


def url(version_id="demo:abc", kind="thumbnail", **kw):
    return media_proxy.media_url(version_id, kind, token(version_id=version_id, kind=kind, **kw))


def test_a_valid_link_streams_the_media_as_the_tokens_user(media):
    client, made = media
    response = client.get(url(login="bob"))
    assert response.status_code == 200 and response.content == b"abc"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=300"
    assert made[0].opened == [("demo:abc", "thumbnail", None, "bob")]
    made[0].closed.assert_called_once()  # the tracker connection is released


def test_range_requests_are_relayed_so_video_can_seek(media):
    client, made = media
    response = client.get(url(kind="movie"), headers={"Range": "bytes=0-2"})
    assert response.status_code == 206 and response.headers["content-range"] == "bytes 0-2/10"
    assert made[0].opened[0][2] == "bytes=0-2"


def test_only_safe_tracker_headers_reach_the_browser():
    # AyonProvider.open_media whitelists headers; the route adds only Cache-Control.
    from dna.prodtrack_providers.ayon import AyonProvider

    response = mock.Mock(status_code=200, headers={"Content-Type": "x", "Set-Cookie": "s=1", "X-Internal": "1"})
    response.iter_content.return_value = iter([])
    client = mock.Mock()
    client.request.return_value = response
    provider = AyonProvider(client=client)
    provider._client = client
    stream = provider.open_media("demo:abc", "thumbnail")
    assert stream.headers == {"Content-Type": "x"}


@pytest.mark.parametrize("query", ["token=forged.token", "token=", "token=x"])
def test_a_forged_link_is_403(media, query):
    client, made = media
    assert client.get(f"/media/versions/demo:abc/thumbnail?{query}").status_code == 403
    assert made == []  # no tracker call was made


def test_a_token_for_another_version_or_kind_is_403(media):
    client, made = media
    good = url(version_id="demo:abc")
    assert client.get(good.replace("demo:abc", "demo:other")).status_code == 403
    assert client.get(good.replace("/thumbnail", "/movie")).status_code == 403
    assert made == []


def test_an_expired_link_is_403_with_a_helpful_message(media):
    client, _ = media
    expired = media_proxy.media_url("demo:abc", "thumbnail", token(ttl=-10))
    response = client.get(expired)
    assert response.status_code == 403 and "expired" in response.json()["detail"]


def test_a_link_without_a_token_is_a_422_and_an_unknown_kind_is_422(media):
    client, _ = media
    assert client.get("/media/versions/demo:abc/thumbnail").status_code == 422
    assert client.get("/media/versions/demo:abc/source?token=x").status_code == 422


def test_media_the_tracker_does_not_have_is_404(media):
    client, _ = media
    assert client.get(url(version_id="demo:none", kind="movie")).status_code == 404


def test_a_connector_that_serves_media_directly_is_404_not_500(media, monkeypatch):
    client, _ = media
    monkeypatch.setattr(main, "get_prodtrack_provider", lambda **kw: mock.Mock(open_media=mock.Mock(side_effect=NotImplementedError)))
    assert client.get(url()).status_code == 404


# ── version listings carry signed URLs ───────────────────────────────────────


def versions():
    return [
        Version(id="demo:v1", name="a", thumbnail="http://ayon/secret-thumb", movie_path="http://ayon/secret-movie"),
        Version(id="demo:v2", name="b"),  # no media at all
    ]


def test_listings_replace_tracker_urls_with_signed_proxy_urls(media):
    provider = MediaProvider("bob")
    vs = versions()
    main._proxy_media(provider, vs)
    assert vs[0].thumbnail.startswith("/media/versions/demo:v1/thumbnail?token=")
    assert vs[0].movie_path.startswith("/media/versions/demo:v1/movie?token=")
    assert vs[1].thumbnail is None and vs[1].movie_path is None
    assert "ayon" not in vs[0].thumbnail  # the tracker URL never reaches the browser
    claims = verify_token(vs[0].thumbnail.split("token=")[1], version_id="demo:v1", kind="thumbnail", secret=SECRET)
    assert claims["u"] == "bob"


def test_connectors_whose_media_is_directly_usable_are_left_alone(media):
    class Direct:
        MEDIA_REQUIRES_PROXY = False

    vs = versions()
    main._proxy_media(Direct(), vs)
    assert vs[0].thumbnail == "http://ayon/secret-thumb"


def test_the_listing_routes_apply_it_end_to_end(media, monkeypatch):
    client, _ = media

    class Listing(MediaProvider):
        def get_versions_for_playlist(self, playlist_id):
            return versions()

        def get_entity(self, kind, entity_id, resolve_links=True):
            return versions()[0]

    monkeypatch.setattr(main, "get_prodtrack_provider", lambda **kw: Listing("bob"))
    body = client.get("/playlists/demo:L1/versions").json()
    assert body[0]["thumbnail"].startswith("/media/versions/demo:v1/thumbnail?token=")
    assert client.get("/version/demo:v1").json()["movie_path"].startswith("/media/versions/demo:v1/movie?token=")
    # and the link it handed out actually works
    assert client.get(body[0]["thumbnail"]).status_code == 200
