# Copyright 2026 Moritz E. Beber
"""
Unit tests for the view position contract.

`is_behind` and `view_headers` are pure functions, and the cheapest place to
pin the two `None` cases that must not be collapsed into each other:
`at_least is None` means no precondition was given (never behind), while
`current is None` with an `at_least` given means nothing has been processed
yet (always behind).
"""

import pytest
from fastapi import status

from course_subscriptions.view import (
    CURRENT_POSITION_HEADER,
    NOT_FOUND_RESPONSE,
    VIEW_RESPONSES,
    is_behind,
    too_early,
    view_headers,
)


def test_is_behind_is_false_when_no_precondition_is_given() -> None:
    """No `at_least` means no precondition, whatever the current position is."""
    assert is_behind(current=None, at_least=None) is False
    assert is_behind(current=5, at_least=None) is False


def test_is_behind_is_true_when_current_is_none_and_a_minimum_is_given() -> None:
    """Nothing processed yet is always behind a caller's stated minimum."""
    assert is_behind(current=None, at_least=0) is True
    assert is_behind(current=None, at_least=1) is True


def test_is_behind_is_true_when_current_is_less_than_at_least() -> None:
    """A view that has not yet reached the caller's minimum is behind."""
    assert is_behind(current=1, at_least=2) is True


def test_is_behind_is_false_when_current_equals_at_least() -> None:
    """A view exactly at the caller's minimum has caught up."""
    assert is_behind(current=2, at_least=2) is False


def test_is_behind_is_false_when_current_exceeds_at_least() -> None:
    """A view ahead of the caller's minimum has caught up."""
    assert is_behind(current=3, at_least=2) is False


def test_view_headers_omits_the_position_header_when_current_is_none() -> None:
    """An undefined position must never be reported as a literal `0` or `null`."""
    headers = view_headers(None)
    assert CURRENT_POSITION_HEADER not in headers
    assert headers["Cache-Control"] == "no-store"


def test_view_headers_reports_the_position_when_current_is_known() -> None:
    """A known position is reported as a string header value."""
    headers = view_headers(7)
    assert headers[CURRENT_POSITION_HEADER] == "7"
    assert headers["Cache-Control"] == "no-store"


def test_too_early_returns_an_empty_425_carrying_the_current_position() -> None:
    """The 425 response is empty-bodied but still reports how far behind it is."""
    response = too_early(4)
    assert response.status_code == 425
    assert response.body == b""
    assert response.headers[CURRENT_POSITION_HEADER] == "4"
    assert response.headers["Cache-Control"] == "no-store"


def test_too_early_omits_the_position_header_when_current_is_none() -> None:
    """A 425 for an empty store still omits the header, not a placeholder."""
    response = too_early(None)
    assert response.status_code == 425
    assert CURRENT_POSITION_HEADER not in response.headers


@pytest.mark.parametrize(
    "documented",
    [
        pytest.param(VIEW_RESPONSES[status.HTTP_200_OK], id="200"),
        pytest.param(VIEW_RESPONSES[status.HTTP_425_TOO_EARLY], id="425"),
        pytest.param(NOT_FOUND_RESPONSE, id="404"),
    ],
)
def test_every_documented_view_response_declares_the_position_header(
    documented: dict[str, object],
) -> None:
    """
    Every response a view can send must declare the header in the spec.

    The routes send `X-Current-Position` on all three, but a header the code
    emits and the spec omits is invisible to any generated client — and no
    request-level test catches that, because the response itself is correct.
    On the 425 in particular the header is the whole point: it is how a polling
    client sizes its next wait.
    """
    assert CURRENT_POSITION_HEADER in documented["headers"]


def test_view_responses_documents_only_the_status_codes_every_view_can_send() -> None:
    """
    A collection view has no absence case, so 404 is opt-in, not built in.

    Folding 404 into `VIEW_RESPONSES` would document a response that a
    catalogue or search view can never return; such a view spreads in
    `NOT_FOUND_RESPONSE` only when it genuinely has an entity to be missing.
    """
    assert set(VIEW_RESPONSES) == {
        status.HTTP_200_OK,
        status.HTTP_425_TOO_EARLY,
    }
