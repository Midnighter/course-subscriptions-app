# Copyright 2026 Moritz E. Beber
"""
Unit tests for the authentication and authorisation module.

`get_principal` is exercised in the integration suite instead: what is worth
proving about it is that the contextvar it sets reaches the route handler and
lands on a recorded event, and that needs a live ASGI stack to mean anything.

The `require_*` dependencies are coroutines driven here with `asyncio.run`,
since the rule each enforces is pure and needs no event loop of its own.
"""

import asyncio

import pytest
from fastapi import HTTPException

from course_subscriptions.auth import (
    COURSES_MANAGE,
    REGISTRATIONS_WRITE,
    SUBSCRIPTIONS_WRITE,
    Principal,
    PrincipalType,
    parse_token,
    require_authenticated,
    require_course_manager,
    require_registrar,
    require_student_self,
)

_STUDENT_ID = "STU-2026-0042"
_OTHER_STUDENT_ID = "STU-2026-0099"


def _student(student_id: str = _STUDENT_ID) -> Principal:
    return Principal(
        principal_id=student_id,
        principal_type=PrincipalType.USER,
        scopes=frozenset({SUBSCRIPTIONS_WRITE}),
    )


def _course_manager() -> Principal:
    return Principal(
        principal_id="MGR-2026-0001",
        principal_type=PrincipalType.USER,
        scopes=frozenset({COURSES_MANAGE}),
    )


def _registrar() -> Principal:
    return Principal(
        principal_id="registrar",
        principal_type=PrincipalType.SERVICE,
        scopes=frozenset({REGISTRATIONS_WRITE}),
    )


def test_parse_token_reads_every_claim():
    """The three fake claims map onto the three fields."""
    principal = parse_token(f"{_STUDENT_ID}|user|subscriptions:write courses:manage")

    assert principal.principal_id == _STUDENT_ID
    assert principal.principal_type is PrincipalType.USER
    assert principal.scopes == frozenset({SUBSCRIPTIONS_WRITE, COURSES_MANAGE})


def test_parse_token_reads_a_service_principal():
    """A machine credential differs only in its type, not its shape."""
    principal = parse_token("registrar|service|registrations:write")

    assert principal.principal_type is PrincipalType.SERVICE


def test_parse_token_accepts_no_scopes():
    """
    An empty scope segment is a principal with no permissions, not an error.

    It is the credential a real deployment issues for a token that has been
    authenticated but granted nothing, and every `require_*` must reject it on
    the authorisation rule rather than mistaking it for a malformed token.
    """
    principal = parse_token(f"{_STUDENT_ID}|user|")

    assert principal.scopes == frozenset()


def test_parse_token_keeps_a_scope_containing_a_colon():
    """
    Scopes are colon-namespaced, which is why the separator is a pipe.

    A colon separator would split `courses:manage` down the middle and hand
    back a principal holding a scope nobody granted.
    """
    assert parse_token("MGR|user|courses:manage").scopes == {COURSES_MANAGE}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "STU-2026-0042",
        "STU-2026-0042|user",
        "STU-2026-0042|user|scope|extra",
        "|user|",
        "   |user|",
    ],
)
def test_parse_token_rejects_a_malformed_credential(raw: str):
    """
    A credential that names no principal is a 401, never a 403.

    The distinction is not cosmetic: 403 would tell a caller they had been
    identified and refused, when in fact they were never identified at all.
    """
    with pytest.raises(HTTPException) as exc_info:
        parse_token(raw)

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers["WWW-Authenticate"] == "Bearer"


def test_parse_token_rejects_an_unknown_principal_type():
    """A type outside the enum is refused rather than coerced to a default."""
    with pytest.raises(HTTPException) as exc_info:
        parse_token("someone|robot|scope")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "unknown_principal_type"


def test_require_authenticated_admits_either_actor():
    """The catalogue asks for identity and nothing more."""
    for principal in (_student(), _course_manager(), _registrar()):
        assert asyncio.run(require_authenticated(principal)) is principal


def test_require_course_manager_admits_a_manager():
    """The scope holder passes and is handed back unchanged."""
    principal = _course_manager()

    assert asyncio.run(require_course_manager(principal)) is principal


def test_require_course_manager_rejects_a_student():
    """A student holds no `courses:manage`, whatever else they hold."""
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_course_manager(_student()))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "missing_scope"


def test_require_course_manager_rejects_a_service():
    """
    A machine cannot act as a course manager even holding the scope.

    The board puts Register Course behind a Course Manager *screen*; a service
    reaching it means a credential has been mixed up somewhere upstream.
    """
    impostor = Principal(
        principal_id="registrar",
        principal_type=PrincipalType.SERVICE,
        scopes=frozenset({COURSES_MANAGE}),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_course_manager(impostor))

    assert exc_info.value.detail == "not_a_user"


def test_require_student_self_admits_the_account_owner():
    """A student acting on their own account passes."""
    principal = _student()

    assert asyncio.run(require_student_self(_STUDENT_ID, principal)) is principal


def test_require_student_self_rejects_another_students_account():
    """
    The whole point of the rule: one student cannot act as another.

    Without this the subscribe and unsubscribe routes would let any
    authenticated student mutate any other student's subscriptions.
    """
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_student_self(_OTHER_STUDENT_ID, _student()))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "not_your_account"


def test_require_student_self_rejects_a_service():
    """A machine has no student account to act on, whatever id it presents."""
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_student_self("registrar", _registrar()))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "not_a_user"


def test_require_registrar_admits_the_registrar():
    """The registrar's own credential passes."""
    principal = _registrar()

    assert asyncio.run(require_registrar(principal)) is principal


def test_require_registrar_rejects_a_human():
    """
    A person cannot post to the webhook, even holding the scope.

    The webhook records an *external* fact the registrar has already decided;
    a human calling it would be asserting something no upstream system said.
    """
    impostor = Principal(
        principal_id=_STUDENT_ID,
        principal_type=PrincipalType.USER,
        scopes=frozenset({REGISTRATIONS_WRITE}),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_registrar(impostor))

    assert exc_info.value.detail == "not_a_service"


def test_require_registrar_rejects_a_service_without_the_scope():
    """Being a machine is not on its own sufficient."""
    unscoped = Principal(
        principal_id="some-other-service",
        principal_type=PrincipalType.SERVICE,
        scopes=frozenset(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(require_registrar(unscoped))

    assert exc_info.value.detail == "missing_scope"
