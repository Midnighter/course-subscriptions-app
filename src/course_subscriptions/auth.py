# Copyright 2026 Moritz E. Beber
"""
Provide the (currently faked) authentication and authorisation surface.

This module stands in for an OAuth2 resource server. Every route that a human
or a machine calls on its own behalf depends on one of the `require_*`
callables below, and every event recorded inside such a request carries the
caller's identity in its metadata.

The fake is deliberately swap-shaped: `parse_token` is the *only* thing that
would change when real token verification arrives. It takes the raw bearer
credential and returns a `Principal`; whether that means splitting a string or
verifying a JWT signature against a JWKS endpoint is invisible to every
dependency, route, and test that builds on it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from eventsourcing.domain import put_metadata_in_context
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from course_subscriptions.metadata import PRINCIPAL_ID_KEY, PRINCIPAL_TYPE_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

COURSES_MANAGE = "courses:manage"
SUBSCRIPTIONS_WRITE = "subscriptions:write"
REGISTRATIONS_WRITE = "registrations:write"

# The separator is a pipe rather than a colon because scopes contain colons.
_CLAIM_SEPARATOR = "|"
_CLAIM_COUNT = 3

# RFC 7235 requires a 401 to say how to authenticate. Every rejection below
# that is a 401 rather than a 403 carries it.
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}

_bearer = HTTPBearer(description="A fake OAuth2 token: `<id>|<type>|<scopes>`.")


class PrincipalType(StrEnum):
    """
    The kind of actor a token authenticates.

    OAuth2 itself has no such notion - RFC 6749 only distinguishes *grants*,
    and the machine case is simply the client credentials grant, where `sub`
    carries a client id rather than a person. Every vendor therefore invented
    its own claim for this (Azure AD `idtyp`, AWS Cognito `token_use`, Google
    Cloud IAM's `user:`/`serviceAccount:` member prefixes), so a project-level
    enum is the normal thing to have.

    Deliberately not named `client`: in OAuth2 that word also means the
    application a *human* signs in through, which would make the value
    ambiguous exactly where it needs to be precise.
    """

    USER = "user"
    SERVICE = "service"


class Principal(BaseModel):
    """
    The authenticated caller behind one request.

    Each field stands in for the JWT claim a real deployment would read it
    from, so the eventual swap is a matter of renaming in one function:

    - `principal_id` is `sub`, the subject identifier. For a student it *is*
      the domain's own `student_id`, which is what makes the ownership rule in
      `require_student_self` expressible at all.
    - `principal_type` is an `idtyp`-style claim (see `PrincipalType`).
    - `scopes` is the space-delimited `scope` claim, split.
    """

    principal_id: str
    principal_type: PrincipalType
    scopes: frozenset[str]


def parse_token(raw: str) -> Principal:
    """
    Turn a raw bearer credential into the principal it names.

    This is the entire swap surface for real authentication: replace the body
    with signature verification and claim extraction, and nothing else in the
    project changes. The fake format is `<principal_id>|<type>|<scopes>`, with
    scopes space-delimited exactly as in an OAuth2 `scope` claim - for
    instance `STU-2026-0042|user|subscriptions:write`.

    Args:
        raw: The credential from the `Authorization: Bearer ...` header.

    Returns:
        The principal the credential names.

    Raises:
        HTTPException: 401, when the credential is malformed or names an
            unknown principal type. A malformed token is not an authorisation
            failure - the caller has not been identified at all.

    """
    parts = raw.split(_CLAIM_SEPARATOR)
    if len(parts) != _CLAIM_COUNT:
        msg = "malformed_token"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=msg,
            headers=_BEARER_CHALLENGE,
        )

    principal_id, raw_type, raw_scopes = (part.strip() for part in parts)
    if not principal_id:
        msg = "missing_principal_id"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=msg,
            headers=_BEARER_CHALLENGE,
        )

    try:
        principal_type = PrincipalType(raw_type)
    except ValueError as exc:
        msg = "unknown_principal_type"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=msg,
            headers=_BEARER_CHALLENGE,
        ) from exc

    return Principal(
        principal_id=principal_id,
        principal_type=principal_type,
        scopes=frozenset(raw_scopes.split()),
    )


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials, Security(_bearer)],
) -> AsyncIterator[Principal]:
    """
    Identify the caller and seed the metadata their events will carry.

    This is the third and last place metadata is seeded, alongside
    `MetadataMiddleware` (which knows the request) and `CourseSubscriptionsApp.do`
    (which knows the unit of work). It is the only place that knows the caller.
    Routes and slices still never touch metadata.

    It must stay an `async def` *generator*. A sync `def` dependency runs in
    the threadpool on a copied context, so the contextvar set here would never
    reach the route - and the failure is silent: events would simply be
    recorded with no principal, with no test failing and no error logged. The
    `with` block also restores the context afterwards, which a plain `async
    def` returning a value could not do.

    Args:
        credentials: The bearer credential FastAPI extracted from the request.

    Yields:
        The authenticated principal, for the duration of the request.

    """
    principal = parse_token(credentials.credentials)
    with put_metadata_in_context(
        {
            PRINCIPAL_ID_KEY: principal.principal_id,
            PRINCIPAL_TYPE_KEY: principal.principal_type.value,
        },
    ):
        yield principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def require_authenticated(principal: CurrentPrincipal) -> Principal:
    """
    Require only that the caller is identified.

    For the course catalogue, which the board shows on both a Student screen
    and a Course Manager screen, with no rule distinguishing them.

    Args:
        principal: The authenticated caller.

    Returns:
        The caller, unchanged.

    """
    return principal


async def require_course_manager(principal: CurrentPrincipal) -> Principal:
    """
    Require a human course manager.

    Args:
        principal: The authenticated caller.

    Returns:
        The caller, once permitted.

    Raises:
        HTTPException: 403, when the caller is a service or holds no
            `courses:manage` scope.

    """
    if principal.principal_type is not PrincipalType.USER:
        msg = "not_a_user"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
    if COURSES_MANAGE not in principal.scopes:
        msg = "missing_scope"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
    return principal


async def require_student_self(
    student_id: str,
    principal: CurrentPrincipal,
) -> Principal:
    """
    Require a student acting on their own account.

    `student_id` is declared here rather than read off the request, so FastAPI
    resolves it from the route's path and hands it in - which is what lets one
    dependency serve every `/students/{student_id}/...` route without any of
    them repeating the comparison.

    Args:
        student_id: The account named in the path.
        principal: The authenticated caller.

    Returns:
        The caller, once permitted.

    Raises:
        HTTPException: 403, when the caller is a service or names a different
            account than the one in the path.

    """
    if principal.principal_type is not PrincipalType.USER:
        msg = "not_a_user"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
    if principal.principal_id != student_id:
        msg = "not_your_account"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
    return principal


async def require_registrar(principal: CurrentPrincipal) -> Principal:
    """
    Require the registrar's machine credential.

    The webhook is the one route no first-party client should ever call, so it
    admits a service principal and rejects a human outright rather than merely
    checking a scope.

    Args:
        principal: The authenticated caller.

    Returns:
        The caller, once permitted.

    Raises:
        HTTPException: 403, when the caller is a user or holds no
            `registrations:write` scope.

    """
    if principal.principal_type is not PrincipalType.SERVICE:
        msg = "not_a_service"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
    if REGISTRATIONS_WRITE not in principal.scopes:
        msg = "missing_scope"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
    return principal
