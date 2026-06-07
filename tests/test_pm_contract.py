"""Consumer-side CONTRACT-CONFORMANCE test for the position-manager funding-arb API.

The only coupling between this app (the funding-arb SIGNAL generator) and
``mochi-position-manager`` (the PROVIDER that owns the API) is the HTTP contract.
The canonical contract is the provider's OpenAPI spec; a PINNED copy is vendored
at ``tests/contract/openapi-funding-arb.yaml`` (refresh with ``make vendor-contract``).

This test makes contract drift fail HERE, automatically, instead of relying on a
human to remember. It exercises the REAL ``PMClient._request`` (by routing
``httpx.Client`` through a capturing ``httpx.MockTransport`` — the same pattern as
``tests/test_approval.py``) and asserts the actual outgoing OPEN/CLOSE requests
conform to the vendored schemas:

  * correct method + path,
  * the ``X-Arb-Secret`` auth header is present,
  * the JSON body validates against the pinned ``ArbOpenRequest`` / ``ArbCloseRequest``
    component schemas (required fields, legal enums incl. ``size_mode:"min"``, types,
    and no key outside the contract's declared properties),
  * the client parses a contract-shaped success response (``ArbOpenResponse`` /
    ``ArbCloseResponse``).

Fully offline: no real network — the MockTransport answers in-process.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from mochi_carry_signal import pm_client as pm_mod
from mochi_carry_signal.config import get_settings

CONTRACT_PATH = Path(__file__).parent / "contract" / "openapi-funding-arb.yaml"

# Base URI under which the whole vendored doc is registered, so the schemas'
# internal ``$ref: '#/components/schemas/...'`` fragments resolve.
_BASE = "urn:funding-arb-contract"


@pytest.fixture(scope="module")
def contract() -> dict:
    """The vendored provider OpenAPI document, parsed once."""
    return yaml.safe_load(CONTRACT_PATH.read_text())


def _validator(contract: dict, schema_name: str) -> Draft202012Validator:
    """A validator for one ``components.schemas`` entry, resolving internal $refs.

    The full document is registered at ``_BASE`` and the root schema is a $ref into
    it, so a nested ``#/components/schemas/LegSpec`` etc. resolves against the same
    document (OpenAPI 3.1 == JSON Schema 2020-12).
    """
    registry = Registry().with_resource(
        _BASE, Resource.from_contents(contract, default_specification=DRAFT202012))
    return Draft202012Validator(
        {"$ref": f"{_BASE}#/components/schemas/{schema_name}"}, registry=registry)


def _properties(contract: dict, schema_name: str) -> set[str]:
    return set(contract["components"]["schemas"][schema_name]["properties"])


@pytest.fixture
def wire(monkeypatch):
    """Run the REAL PMClient on the wire, capturing requests via MockTransport.

    Returns ``(captured, client)`` where ``captured`` is a list of the
    ``httpx.Request`` objects the client actually emitted. The PM answers 200 with
    a contract-shaped ``ArbOpenResponse`` / ``ArbCloseResponse`` so the client's
    real response parsing runs too.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        if path.endswith("/open"):
            key = json.loads(request.content)["idempotency_key"]
            return httpx.Response(200, json={
                "status": "accepted", "arb_id": 4242,
                "idempotency_key": key, "legs": []})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closing", "arb_id": 4242})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    # Inject the mock transport into the client's OWN httpx.Client(...) so the real
    # `_request` code path (URL build, headers, JSON serialization) is exercised.
    real_client_cls = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: real_client_cls(*a, transport=transport, **kw))

    client = pm_mod.PMClient(get_settings())
    # offline = testing or dry_run -> stub. Force the real wire path despite TESTING.
    monkeypatch.setattr(client._s, "testing", False)
    monkeypatch.setattr(client._s, "dry_run", False)
    return captured, client


# --------------------------------------------------------------------------- #
# OPEN
# --------------------------------------------------------------------------- #

def test_open_request_conforms_to_contract(contract, wire):
    captured, client = wire
    key = "sig-2026-06-07T12:00:00Z-BTC-OPEN"

    resp = client.open_arb(idempotency_key=key, asset="BTC")

    # --- method + path + auth header ---
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/funding-arb/open"
    assert req.headers["X-Arb-Secret"] == get_settings().funding_arb_secret

    # --- body validates against the pinned ArbOpenRequest schema ---
    body = json.loads(req.content)
    _validator(contract, "ArbOpenRequest").validate(body)            # required/enum/type
    assert set(body) <= _properties(contract, "ArbOpenRequest")      # no forbidden keys

    # This app's pinned choices (all legal contract values):
    assert body["size_mode"] == "min"
    assert body["asset"] == "BTC"
    assert body["strategy_tag"] == "hl-cash-and-carry"
    assert body["idempotency_key"] == key
    assert "notional" not in body                                    # size_mode=min
    assert "legs" not in body                                        # default HL combo

    # --- client parses a contract-shaped ArbOpenResponse ---
    _validator(contract, "ArbOpenResponse").validate(resp)
    assert resp["status"] == "accepted"
    assert isinstance(resp["arb_id"], int)
    assert resp["idempotency_key"] == key


# --------------------------------------------------------------------------- #
# CLOSE
# --------------------------------------------------------------------------- #

def test_close_request_conforms_to_contract(contract, wire):
    captured, client = wire

    resp = client.close_arb(arb_id=4242)

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/funding-arb/close"
    assert req.headers["X-Arb-Secret"] == get_settings().funding_arb_secret

    body = json.loads(req.content)
    _validator(contract, "ArbCloseRequest").validate(body)
    assert set(body) <= _properties(contract, "ArbCloseRequest")
    assert body == {"arb_id": 4242}

    _validator(contract, "ArbCloseResponse").validate(resp)
    assert resp["status"] in {"closing", "already_closed"}
    assert isinstance(resp["arb_id"], int)


# --------------------------------------------------------------------------- #
# The validator must have TEETH — a contract test that can't fail is worthless.
# --------------------------------------------------------------------------- #

def test_open_validator_rejects_contract_violations(contract):
    v = _validator(contract, "ArbOpenRequest")
    good = {"idempotency_key": "k", "asset": "BTC", "size_mode": "min",
            "strategy_tag": "hl-cash-and-carry"}
    v.validate(good)                                          # sanity: the good shape passes

    with pytest.raises(ValidationError):
        v.validate({**good, "asset": "DOGE"})                 # asset not in enum
    with pytest.raises(ValidationError):
        v.validate({**good, "size_mode": "market"})           # size_mode not in enum
    with pytest.raises(ValidationError):
        v.validate({k: x for k, x in good.items() if k != "idempotency_key"})  # missing required
    with pytest.raises(ValidationError):
        v.validate({**good, "idempotency_key": 123})          # wrong type (int not str)


def test_close_validator_rejects_contract_violations(contract):
    v = _validator(contract, "ArbCloseRequest")
    v.validate({"arb_id": 1})

    with pytest.raises(ValidationError):
        v.validate({})                                        # arb_id required
    with pytest.raises(ValidationError):
        v.validate({"arb_id": 0})                             # exclusiveMinimum 0
    with pytest.raises(ValidationError):
        v.validate({"arb_id": "1"})                           # wrong type (str not int)


# --------------------------------------------------------------------------- #
# Pin the contract SHAPE our client assumes (catches a corrupt / drifted re-vendor).
# --------------------------------------------------------------------------- #

def test_vendored_contract_pins_expected_shape(contract):
    schemas = contract["components"]["schemas"]

    # Auth scheme: the X-Arb-Secret header we send.
    arb_secret = contract["components"]["securitySchemes"]["ArbSecret"]
    assert arb_secret["type"] == "apiKey"
    assert arb_secret["in"] == "header"
    assert arb_secret["name"] == "X-Arb-Secret"

    # Both POST routes exist, are secured, and reference the request schemas we send.
    for path, schema_name in [("/funding-arb/open", "ArbOpenRequest"),
                              ("/funding-arb/close", "ArbCloseRequest")]:
        op = contract["paths"][path]["post"]
        assert {"ArbSecret": []} in op["security"]
        ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref == f"#/components/schemas/{schema_name}"

    # The enum values + required fields our PMClient depends on.
    open_req = schemas["ArbOpenRequest"]
    assert set(open_req["required"]) >= {"idempotency_key", "asset"}
    assert "min" in open_req["properties"]["size_mode"]["enum"]
    assert set(open_req["properties"]["asset"]["enum"]) >= {"BTC", "ETH", "SOL"}

    assert schemas["ArbCloseRequest"]["required"] == ["arb_id"]
