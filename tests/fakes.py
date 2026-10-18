"""Offline stand-ins for PRAW objects and Reddit's HTTP answers, built from the fabricated
JSON fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import requests

FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1_800_000_000
PAGE = 100  # items per listing page


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def obj(data: dict) -> SimpleNamespace:
    return SimpleNamespace(**data)


def comment(data: dict) -> SimpleNamespace:
    data = dict(data)
    data["replies"] = [comment(r) for r in data.get("replies", [])]
    return obj(data)


class FakeForest(list):
    def replace_more(self, limit=None):
        self.replace_more_limit = limit


class FakeReddit:
    """Mimics the few PRAW calls the reader uses and counts the requests they would send:
    one per listing page of 100, one per comment fetch, info() call, or /api/v1/me."""

    def __init__(self, posts=None, comments=None, info=None, errors=None, me_error=None):
        self.posts = {"testsub": [obj(p) for p in (posts or [])]}
        self.comment_trees = comments or {}
        self.info_items = [obj(i) for i in (info or [])]
        self.errors = errors or {}
        self.me_error = me_error
        self.requests = 0
        self.me_calls = 0
        self.info_calls: list[list[str]] = []
        self.user = SimpleNamespace(me=self._me)

    def _me(self):
        self.requests += 1
        self.me_calls += 1
        if self.me_error:
            raise self.me_error
        return SimpleNamespace(name="someone")

    def subreddit(self, name):
        reddit = self

        class Sub:
            def new(self, limit=None):
                reddit.requests += 1  # the first page
                if name in reddit.errors:
                    raise reddit.errors[name]
                for i, post in enumerate(reddit.posts.get(name, [])[:limit]):
                    if i and i % PAGE == 0:
                        reddit.requests += 1  # the next page
                    yield post

        return Sub()

    def submission(self, id):
        self.requests += 1
        tree = FakeForest(comment(c) for c in self.comment_trees.get(id, []))
        return SimpleNamespace(id=id, comments=tree, comment_sort=None)

    def info(self, fullnames):
        self.requests += 1
        self.info_calls.append(list(fullnames))
        wanted = {n.split("_", 1)[1] for n in fullnames}
        return [i for i in self.info_items if i.id in wanted]


def http_response(status: int, body) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.headers["content-type"] = "application/json"
    r.headers.update({"x-ratelimit-remaining": "990", "x-ratelimit-used": "10"})
    r.headers["x-ratelimit-reset"] = "500"
    return r


class FakeHTTP:
    """Replaces requests.Session.request, so real PRAW runs without any network: answers
    the token endpoint, /r/<name>/new listings, and /api/info from the fixtures."""

    def __init__(self, listings, info=(), fail_listing: int = 0, on_listing=None):
        self.listings = listings
        self.info = {i["id"]: i for i in info}
        self.fail_listing = fail_listing  # answer this many listing requests with a 503
        self.on_listing = on_listing
        self.calls: list[str] = []

    def install(self, monkeypatch) -> FakeHTTP:
        monkeypatch.setattr(requests.Session, "request", lambda _s, *a, **k: self(*a, **k))
        return self

    def __call__(self, method, url, **kwargs):
        path = urlparse(url).path.rstrip("/")
        self.calls.append(path)
        if path == "/api/v1/access_token":
            token = {"access_token": "t", "expires_in": 3600, "scope": "*", "token_type": "bearer"}
            return http_response(200, token)
        if path == "/api/info":
            wanted = kwargs["params"]["id"].split(",")
            children = [
                {"kind": name[:2], "data": self.info[name[3:]]}
                for name in wanted
                if name[3:] in self.info
            ]
            return http_response(200, {"kind": "Listing", "data": {"children": children}})
        name = path.removeprefix("/r/").removesuffix("/new")
        if name not in self.listings:
            return http_response(404, {})
        if self.on_listing:
            self.on_listing(name)
        if self.fail_listing:
            self.fail_listing -= 1
            return http_response(503, {})
        children = [{"kind": "t3", "data": p} for p in self.listings[name]]
        return http_response(
            200, {"kind": "Listing", "data": {"children": children, "after": None}}
        )
