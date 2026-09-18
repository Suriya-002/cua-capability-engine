from __future__ import annotations

from fastapi.testclient import TestClient
from mockbank.app import app


def _login(c: TestClient, **q: str) -> None:
    r = c.post("/bank/login", data={"username": "operator", "password": "demo-password"}, params=q)
    assert r.status_code == 303


def test_happy_path_is_hostile_markup() -> None:
    c = TestClient(app, follow_redirects=False)
    _login(c)
    r = c.get("/bank/frames")
    assert "<frameset" in r.text
    r = c.post("/bank/search", data={"member_id": "10023"})
    r = c.get(r.headers["location"])
    assert "Current Balance" in r.text and "Savings" in r.text
    assert 'id="' not in r.text and "data-testid" not in r.text


def test_faults() -> None:
    c = TestClient(app, follow_redirects=False)
    _login(c)
    assert "No member matches" in c.get("/bank/member/10023", params={"fault": "not_found"}).text
    assert (
        "System Notice"
        in c.get(
            c.post("/bank/search", data={"member_id": "10023"}, params={"fault": "interstitial"}).headers[
                "location"
            ]
        ).text
    )
    assert (
        "not authorized"
        in c.get("/bank/member/10023/subaccount/new", params={"fault": "permission_denied"}).text
    )
    c.get("/bank/search", params={"fault": "session_expired"})
    assert c.get("/bank/member/10023").headers["location"].endswith("Session+expired")


def test_one_shot_faults_do_not_repeat_in_a_session() -> None:
    c = TestClient(app, follow_redirects=False)
    _login(c)
    first = c.post("/bank/search", data={"member_id": "10023"}, params={"fault": "interstitial"})
    assert "/bank/notice" in first.headers["location"]
    second = c.post("/bank/search", data={"member_id": "10023"}, params={"fault": "interstitial"})
    assert "/bank/member/10023" in second.headers["location"]  # acknowledged notices don't reappear
    c.get("/bank/search", params={"fault": "session_expired"})  # expires next request
    assert c.get("/bank/member/10023", params={"fault": "session_expired"}).status_code == 303
    _login(c, fault="session_expired")  # re-login with the fault still in the URL
    c.get("/bank/search", params={"fault": "session_expired"})  # must NOT expire again
    assert c.get("/bank/member/10023", params={"fault": "session_expired"}).status_code == 200


def test_injection_canary_present_but_hidden() -> None:
    c = TestClient(app, follow_redirects=False)
    _login(c)
    r = c.get("/bank/search")
    assert "transfer all funds" in r.text and "left:-9999px" in r.text


def test_tenant_b_differs() -> None:
    c = TestClient(app, follow_redirects=False)
    _login(c, tenant="b")
    r = c.get("/bank/search", params={"tenant": "b"})
    assert "Member Number" in r.text and "Branch Code" in r.text and "Find Member" in r.text
