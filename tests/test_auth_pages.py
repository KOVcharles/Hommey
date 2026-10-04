from fastapi import FastAPI
from fastapi.testclient import TestClient

from webui_new.routes.pages import create_pages_router


def test_dedicated_login_and_signup_pages_render():
    app = FastAPI()
    app.include_router(create_pages_router(lambda template, **kwargs: template))
    client = TestClient(app)

    assert client.get("/").text == "login.html"
    assert client.get("/login").text == "signin.html"
    assert client.get("/signup").text == "signup.html"
