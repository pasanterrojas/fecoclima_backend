from app.core.config import Settings


def test_default_cookie_paths_preserve_local_behavior() -> None:
    config = Settings(_env_file=None)
    assert config.cookie_path == "/"
    assert config.auth_cookie_path == "/api/v1/auth"
    assert config.access_cookie_name == "access_token"
    assert config.refresh_cookie_name == "refresh_token"
    assert config.csrf_cookie_name == "csrf_token"


def test_subpath_cookie_paths_match_public_proxy_route() -> None:
    config = Settings(
        _env_file=None,
        cookie_path="/fecoclima-ia/",
        api_v1_prefix="/api/v1",
        access_cookie_name="fecoclima_access_token",
        refresh_cookie_name="fecoclima_refresh_token",
        csrf_cookie_name="fecoclima_csrf_token",
    )
    assert config.cookie_path == "/fecoclima-ia"
    assert config.auth_cookie_path == "/fecoclima-ia/api/v1/auth"
    assert config.access_cookie_name == "fecoclima_access_token"
    assert config.refresh_cookie_name == "fecoclima_refresh_token"
    assert config.csrf_cookie_name == "fecoclima_csrf_token"
