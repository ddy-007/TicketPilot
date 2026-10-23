import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import ConfigurationError, Settings
from app.execution.api import identity, router
from simulator.config import SimulatorSettings

OPERATOR = "o" * 40
APPROVER = "a" * 40
INVALID_TOKENS = [
    pytest.param("", id="empty"),
    pytest.param("s" * 31, id="too-short"),
    pytest.param("s" * 257, id="too-long"),
    pytest.param(" " * 40, id="only-spaces"),
    pytest.param(" " + "s" * 40, id="leading-space"),
    pytest.param("s" * 40 + " ", id="trailing-space"),
    pytest.param("s" * 20 + " " + "s" * 20, id="embedded-space"),
    pytest.param("s" * 40 + "\t", id="tab"),
    pytest.param("s" * 40 + "\n", id="newline"),
    pytest.param("s" * 40 + "\r", id="carriage-return"),
    pytest.param("s" * 40 + "\x00", id="nul"),
    pytest.param("s" * 40 + "\x7f", id="del"),
    pytest.param("s" * 40 + "\u4e2d", id="non-ascii"),
    pytest.param("replace-with-at-least-32-random-characters", id="placeholder"),
    pytest.param("RePlAcE-WiTh-a-different-at-least-32-character-value", id="mixed-placeholder"),
]


def client_for(operator=OPERATOR, approver=APPROVER):
    app = FastAPI()
    app.state.settings = Settings(
        _env_file=None,
        app_database_url="postgresql+psycopg://unused",
        checkpoint_database_url="postgresql://unused",
        demo_operator_token=operator,
        demo_approver_token=approver,
        demo_tenant_id="test-tenant",
    )
    app.include_router(router)

    @app.get("/identity")
    def current_identity(who=Depends(identity)):  # noqa: B008
        return who

    return TestClient(app)


@pytest.mark.parametrize("invalid", INVALID_TOKENS)
@pytest.mark.parametrize("invalid_role", ["operator", "approver"])
def test_invalid_configuration_blocks_both_roles_without_exposing_tokens(invalid, invalid_role):
    operator, approver = (invalid, APPROVER) if invalid_role == "operator" else (OPERATOR, invalid)
    with client_for(operator, approver) as client:
        # Even the other, valid credential must fail closed before business access.
        valid_other = APPROVER if invalid_role == "operator" else OPERATOR
        for presented in (valid_other, "unknown", None):
            headers = {} if presented is None else {"Authorization": "Bearer " + presented}
            response = client.get("/identity", headers=headers)
            assert response.status_code == 503
            assert response.json() == {"detail": "DEMO_AUTH_NOT_CONFIGURED"}


@pytest.mark.parametrize(
    "token",
    [pytest.param(OPERATOR, id="random-like"), pytest.param("!" * 32, id="minimum")],
)
def test_identical_role_tokens_fail_closed(token):
    with client_for(token, token) as client:
        response = client.get("/identity", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 503
    assert response.json() == {"detail": "DEMO_AUTH_NOT_CONFIGURED"}


def test_documented_placeholder_cannot_authenticate_as_approver():
    operator = "replace-with-at-least-32-random-characters"
    approver = "replace-with-a-different-at-least-32-character-value"
    with client_for(operator, approver) as client:
        response = client.get("/identity", headers={"Authorization": "Bearer " + approver})
    assert response.status_code == 503
    assert response.json() == {"detail": "DEMO_AUTH_NOT_CONFIGURED"}


@pytest.mark.parametrize("length", [32, 256])
@pytest.mark.parametrize("role", ["operator", "approver"])
def test_valid_boundary_tokens_map_to_exact_role(length, role):
    operator, approver = "!" + "o" * (length - 2) + "~", "~" + "a" * (length - 2) + "!"
    token = operator if role == "operator" else approver
    with client_for(operator, approver) as client:
        response = client.get("/identity", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json() == {"user": role, "role": role, "tenant": "test-tenant"}


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("Bearer ", id="missing-token"),
        pytest.param("Bearer " + "x" * 40, id="unknown"),
        pytest.param("bearer " + OPERATOR, id="wrong-case-scheme"),
        pytest.param("Basic " + OPERATOR, id="wrong-scheme"),
        pytest.param("Bearer" + OPERATOR, id="missing-space"),
        pytest.param("Bearer  " + OPERATOR, id="extra-space"),
        pytest.param("Bearer " + OPERATOR + " ", id="trailing-space"),
        pytest.param("Bearer " + OPERATOR + "x", id="prefix-match"),
    ],
)
def test_invalid_bearer_returns_401_without_exposing_tokens(authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    with client_for() as client:
        response = client.get("/identity", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "UNAUTHORIZED"}


@pytest.mark.parametrize("invalid", INVALID_TOKENS)
@pytest.mark.parametrize("invalid_role", ["read", "write"])
def test_simulator_keeps_the_same_token_configuration_rules(invalid, invalid_role):
    read, write = (invalid, "w" * 40) if invalid_role == "read" else ("r" * 40, invalid)
    settings = SimulatorSettings(
        _env_file=None,
        simulator_database_url="postgresql+psycopg://unused",
        simulator_read_token=read,
        simulator_write_token=write,
    )
    with pytest.raises(ConfigurationError) as caught:
        settings.require_tokens()
    assert str(caught.value) == (
        "Simulator tokens must be distinct, contain 32-256 printable ASCII "
        "characters without whitespace, and must not be example placeholders."
    )


@pytest.mark.parametrize("length", [32, 256])
def test_simulator_accepts_boundary_tokens_and_rejects_identical_tokens(length):
    settings = SimulatorSettings(
        _env_file=None,
        simulator_database_url="postgresql+psycopg://unused",
        simulator_read_token="r" * length,
        simulator_write_token="w" * length,
    )
    settings.require_tokens()
    settings.simulator_write_token = settings.simulator_read_token
    with pytest.raises(ConfigurationError):
        settings.require_tokens()
