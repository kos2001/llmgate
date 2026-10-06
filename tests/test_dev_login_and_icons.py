from __future__ import annotations

import io
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import MockServer, sso_settings
from llmgate.main import STATIC_DIR, create_app
from mockoidc.server import create_app as create_oidc


# ------------------------------------------------------------------------ dev auto-login (real OIDC flow, no form)

@pytest.fixture
def auto_idp():
    srv = MockServer(make_app=lambda port: create_oidc(f"http://127.0.0.1:{port}", auto_login="alice")).start()
    yield srv
    srv.stop()


def _authorize(location: str) -> httpx.Response:
    return httpx.get(location, follow_redirects=False)


def test_auth_config_advertises_dev_autologin_only_when_asked_and_sso_is_on(idp):
    with TestClient(create_app(sso_settings(idp, dev_autologin=True)), base_url="http://127.0.0.1") as gw:
        assert gw.get("/auth/config").json()["dev_autologin"] is True
    with TestClient(create_app(sso_settings(idp)), base_url="http://127.0.0.1") as gw:
        assert gw.get("/auth/config").json()["dev_autologin"] is False  # production default
    from conftest import Settings

    with TestClient(create_app(Settings(database_path=":memory:", dev_autologin=True, _env_file=None))) as gw:
        assert gw.get("/auth/config").json()["dev_autologin"] is False  # no SSO configured: nothing to auto-login with


def test_auto_login_idp_signs_in_without_a_picker_and_still_does_the_full_flow(auto_idp):
    with TestClient(create_app(sso_settings(auto_idp)), base_url="http://127.0.0.1") as gw:
        start = gw.get("/auth/oidc/login", follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        assert q["code_challenge_method"] == ["S256"] and "prompt" not in q  # still PKCE + state + nonce
        approved = _authorize(start.headers["location"])
        assert approved.status_code == 302 and "code=" in approved.headers["location"]  # approved at once: no user picker
        done = gw.get(approved.headers["location"], follow_redirects=False)
        assert done.status_code == 302 and done.headers["location"] == "/dashboard/"
        me = gw.get("/auth/me").json()
        assert me["email"] == "alice@example.com" and me["role"] == "admin" and me["kind"] == "session"


def test_select_account_brings_back_the_picker_so_another_user_can_sign_in(auto_idp):
    with TestClient(create_app(sso_settings(auto_idp)), base_url="http://127.0.0.1") as gw:
        start = gw.get("/auth/oidc/login?prompt=select_account", follow_redirects=False)
        assert parse_qs(urlparse(start.headers["location"]).query)["prompt"] == ["select_account"]
        picker = _authorize(start.headers["location"])
        assert picker.status_code == 200 and "Carol Viewer" in picker.text  # standard OIDC prompt: choose who to sign in as
        carol = _authorize(start.headers["location"] + "&login_as=carol")
        gw.get(carol.headers["location"], follow_redirects=False)
        assert gw.get("/auth/me").json()["role"] == "viewer"
        # unknown prompt values are ignored rather than forwarded
        weird = gw.get("/auth/oidc/login?prompt=none%0d%0aX-Evil:1", follow_redirects=False)
        assert "prompt" not in parse_qs(urlparse(weird.headers["location"]).query)


def test_dev_command_defaults_to_sso_with_autologin_as_alice():
    import argparse

    from llmgate import dev

    p = argparse.ArgumentParser()
    dev.add_arguments(p)
    a = p.parse_args([])
    assert a.no_sso is False and a.login_as is None and dev.DEFAULTS["login_as"] == "alice" and a.no_seed is False
    assert p.parse_args(["--no-sso", "--as", "carol", "--no-seed"]).login_as == "carol"
    assert dev.ROLE_OF["alice"] == "admin" and dev.ROLE_OF["carol"] == "viewer"
    with pytest.raises(SystemExit):
        p.parse_args(["--as", "mallory"])  # only the mock IdP's known users (or "none")


# ------------------------------------------------------------------------------------------------------- icons

def test_icon_files_exist_with_the_declared_sizes_and_transparency():
    expected = {"icon-192.png": (192, 192), "icon-512.png": (512, 512), "apple-touch-icon.png": (180, 180), "icon-maskable-512.png": (512, 512)}
    for name, size in expected.items():
        img = Image.open(STATIC_DIR / name)
        assert img.size == size, name
    assert Image.open(STATIC_DIR / "icon-512.png").convert("RGBA").getpixel((1, 1))[3] == 0  # rounded tile: transparent corners
    assert Image.open(STATIC_DIR / "apple-touch-icon.png").mode == "RGB"  # iOS wants opaque, it rounds the corners itself
    assert Image.open(STATIC_DIR / "icon-maskable-512.png").convert("RGBA").getpixel((1, 1))[3] == 255  # full-bleed for Android masks
    ico = Image.open(STATIC_DIR / "favicon.ico")
    assert {(16, 16), (32, 32), (48, 48)} <= set(ico.info["sizes"])
    svg = (STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")
    assert svg.startswith("<svg") and 'viewBox="0 0 64 64"' in svg and "<title>llmgate</title>" in svg


def test_icons_are_served_and_linked(gw):
    ico = gw.get("/favicon.ico")  # what every browser requests at the site root, whatever page it is on
    assert ico.status_code == 200 and ico.headers["content-type"].startswith("image/") and ico.content[:4] == b"\x00\x00\x01\x00"
    assert Image.open(io.BytesIO(ico.content)).size[0] in (16, 32, 48)
    svg = gw.get("/favicon.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    page = gw.get("/dashboard/").text
    for needle in ('rel="icon" href="favicon.svg"', 'rel="apple-touch-icon"', 'rel="manifest"', 'name="theme-color"', 'class="logo" src="favicon.svg"'):
        assert needle in page, needle
    for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "manifest.webmanifest"):
        r = gw.get(f"/dashboard/{name}")
        assert r.status_code == 200, name
        assert r.headers["cache-control"] == "no-cache"  # revalidated, so an edited icon/JS is never served stale


def test_web_manifest_is_valid_and_points_at_real_icons(gw):
    manifest = gw.get("/dashboard/manifest.webmanifest").json()
    assert manifest["name"] == "llmgate" and manifest["start_url"] == "/dashboard/" and manifest["display"] == "standalone"
    raster = [i for i in manifest["icons"] if i["type"] == "image/png"]
    assert {i["sizes"] for i in raster} == {"192x192", "512x512"} and any(i.get("purpose") == "maskable" for i in raster)
    for icon in manifest["icons"]:
        r = gw.get("/dashboard/" + icon["src"])
        assert r.status_code == 200
        if icon["type"] == "image/png":
            assert "x".join(map(str, Image.open(io.BytesIO(r.content)).size)) == icon["sizes"]


def test_api_docs_use_the_icon_and_still_work(gw):
    for path, marker in (("/docs", "swagger"), ("/redoc", "redoc")):
        page = gw.get(path)
        assert page.status_code == 200 and "/favicon.svg" in page.text and marker in page.text.lower()
    assert "paths" in gw.get("/openapi.json").json()
