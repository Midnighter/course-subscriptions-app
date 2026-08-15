# Copyright 2026 Moritz E. Beber
# ruff: noqa: T201, PLR2004, ERA001, INP001
"""
Walk a live course_subscriptions server through the core business rules.

Run against a server already listening on http://localhost:8000:

    hatch run dev:python scripts/manual_scenario.py

Registers two courses (capacity 10) and three students (course limit 1),
then reconfirms the same invariants asserted by the pytest integration suite
under tests/integration/course_subscriptions/, including a capacity change.
Exits non-zero and prints the mismatch if the live server disagrees with any
step's expectation.

Every view is read with read-your-writes: the position reported by the last
successful command is sent as X-Position-AtLeast, and a 425 response is retried
every POLL_INTERVAL seconds until the view has caught up.
"""

from __future__ import annotations

import sys
import time

import httpx2

BASE_URL = "http://localhost:8000"

POSITION_AT_LEAST_HEADER = "X-Position-AtLeast"
POLL_INTERVAL = 0.5
POLL_TIMEOUT = 30.0

COURSE_A = "EM-2026-101"
COURSE_B = "EM-2026-102"
UNKNOWN_COURSE = "EM-0000-000"

STUDENT_1 = "STU-2026-0001"
STUDENT_2 = "STU-2026-0002"
STUDENT_3 = "STU-2026-0003"

_FAILURES: list[str] = []
_LAST_POSITION: int | None = None


def expect(
    response: httpx2.Response,
    status: int,
    detail: str | None = None,
    *,
    label: str,
) -> None:
    """Assert a response's status (and optionally detail) and report the result."""
    global _LAST_POSITION  # noqa: PLW0603

    body = response.json() if response.content else None
    actual_detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(body, dict) and isinstance(body.get("position"), int):
        _LAST_POSITION = body["position"]
    ok = response.status_code == status and (detail is None or actual_detail == detail)
    if ok:
        print(f"OK   {label}")
        return
    message = (
        f"{label}: expected status={status} detail={detail!r}, "
        f"got status={response.status_code} detail={actual_detail!r} body={body!r}"
    )
    _FAILURES.append(message)
    print(f"FAIL {message}")


def register_course(
    client: httpx2.Client,
    course_id: str,
    capacity: int,
) -> httpx2.Response:
    """Register a course with the given capacity."""
    return client.post(
        "/courses/register",
        json={
            "course_id": course_id,
            "title": f"Course {course_id}",
            "capacity": capacity,
        },
    )


def register_student(
    client: httpx2.Client,
    student_id: str,
    course_limit: int,
) -> httpx2.Response:
    """Register a student with the given course limit."""
    return client.post(
        "/students/register",
        json={
            "student_id": student_id,
            "name": f"Student {student_id}",
            "course_limit": course_limit,
        },
    )


def subscribe(
    client: httpx2.Client,
    student_id: str,
    course_id: str,
) -> httpx2.Response:
    """Subscribe a student to a course."""
    return client.post(
        f"/students/{student_id}/subscribe-to-course",
        json={"course_id": course_id},
    )


def unsubscribe(
    client: httpx2.Client,
    student_id: str,
    course_id: str,
) -> httpx2.Response:
    """Unsubscribe a student from a course."""
    return client.post(
        f"/students/{student_id}/unsubscribe-from-course",
        json={"course_id": course_id},
    )


def change_capacity(
    client: httpx2.Client,
    course_id: str,
    capacity: int,
) -> httpx2.Response:
    """Change a course's capacity."""
    return client.post(
        f"/courses/{course_id}/change-capacity",
        json={"capacity": capacity},
    )


def read_view(client: httpx2.Client, url: str) -> httpx2.Response:
    """
    Read a view, waiting until it reflects the last command's position.

    Raises:
        TimeoutError: If the view stays behind for longer than POLL_TIMEOUT.

    """
    headers = (
        {}
        if _LAST_POSITION is None
        else {POSITION_AT_LEAST_HEADER: str(_LAST_POSITION)}
    )
    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        response = client.get(url, headers=headers)
        if response.status_code != 425:
            return response
        if time.monotonic() >= deadline:
            message = (
                f"{url} did not reach position {_LAST_POSITION} "
                f"within {POLL_TIMEOUT} seconds"
            )
            raise TimeoutError(message)
        time.sleep(POLL_INTERVAL)


def main() -> int:
    """Run the scenario against BASE_URL and report a pass/fail summary."""
    with httpx2.Client(base_url=BASE_URL) as client:
        expect(client.get("/healthz"), 200, label="server is up")

        # register_course: success + course_already_registered
        expect(
            register_course(client, COURSE_A, 10),
            201,
            label="register course A (capacity 10)",
        )
        expect(
            register_course(client, COURSE_B, 10),
            201,
            label="register course B (capacity 10)",
        )
        expect(
            register_course(client, COURSE_A, 10),
            422,
            "course_already_registered",
            label="re-register course A",
        )

        # register_student: success, and a duplicate submission is still
        # accepted (the route is unconditional; the internal
        # student_already_registered guard lives in the automation, not here)
        for student_id in (STUDENT_1, STUDENT_2, STUDENT_3):
            expect(
                register_student(client, student_id, 1),
                201,
                label=f"register {student_id} (course_limit 1)",
            )
        expect(
            register_student(client, STUDENT_1, 1),
            201,
            label="re-register student 1 (accepted; automation absorbs the duplicate)",
        )

        # course_catalogue: initial listing
        catalogue = read_view(client, "/course-catalogue").json()["courses"]
        by_id = {entry["course_id"]: entry for entry in catalogue}
        assert by_id[COURSE_A]["capacity"] == 10  # noqa: S101
        assert by_id[COURSE_A]["number_of_subscriptions"] == 0  # noqa: S101
        assert by_id[COURSE_B]["capacity"] == 10  # noqa: S101
        print(
            "OK   course catalogue shows both courses at capacity 10, 0 subscriptions",
        )

        # subscribe_student: unknown_course, success, already_subscribed,
        #                    subscription_limit_reached
        expect(
            subscribe(client, STUDENT_3, UNKNOWN_COURSE),
            422,
            "unknown_course",
            label="subscribe to an unregistered course",
        )
        expect(
            subscribe(client, STUDENT_1, COURSE_A),
            201,
            label="student 1 subscribes to course A",
        )
        expect(
            subscribe(client, STUDENT_1, COURSE_A),
            422,
            "already_subscribed",
            label="student 1 subscribes to course A again",
        )
        expect(
            subscribe(client, STUDENT_1, COURSE_B),
            422,
            "subscription_limit_reached",
            label="student 1 (limit 1) subscribes to a second course",
        )
        expect(
            subscribe(client, STUDENT_2, COURSE_A),
            201,
            label="student 2 subscribes to course A",
        )

        catalogue = read_view(client, "/course-catalogue").json()["courses"]
        by_id = {entry["course_id"]: entry for entry in catalogue}
        assert by_id[COURSE_A]["number_of_subscriptions"] == 2  # noqa: S101
        print("OK   course A now has 2 subscriptions")

        # change_course_capacity: success (the required capacity change), same_capacity,
        # capacity_below_subscriptions, unknown_course
        expect(
            change_capacity(client, COURSE_A, 2),
            201,
            label="lower course A capacity 10 -> 2 (equals current subscriptions)",
        )
        expect(
            change_capacity(client, COURSE_A, 2),
            422,
            "same_capacity",
            label="repeat the same capacity change",
        )
        expect(
            subscribe(client, STUDENT_3, COURSE_A),
            422,
            "course_full",
            label="student 3 subscribes to now-full course A",
        )
        expect(
            subscribe(client, STUDENT_3, COURSE_B),
            201,
            label="student 3 subscribes to course B instead",
        )
        expect(
            change_capacity(client, COURSE_A, 0),
            422,
            "capacity_below_subscriptions",
            label="lower course A capacity below its subscriptions",
        )
        expect(
            change_capacity(client, UNKNOWN_COURSE, 5),
            422,
            "unknown_course",
            label="change capacity of an unregistered course",
        )

        # unsubscribe_student: success, not_subscribed, unknown_course
        expect(
            unsubscribe(client, STUDENT_2, COURSE_A),
            201,
            label="student 2 unsubscribes from course A",
        )
        expect(
            unsubscribe(client, STUDENT_2, COURSE_A),
            422,
            "not_subscribed",
            label="student 2 unsubscribes from course A again",
        )
        expect(
            unsubscribe(client, STUDENT_2, UNKNOWN_COURSE),
            422,
            "unknown_course",
            label="unsubscribe from an unregistered course",
        )
        expect(
            subscribe(client, STUDENT_2, COURSE_A),
            201,
            label="student 2 re-subscribes to course A",
        )

        # student_course_subscriptions: per-student projections
        for student_id, expected_courses in (
            (STUDENT_1, [COURSE_A]),
            (STUDENT_2, [COURSE_A]),
            (STUDENT_3, [COURSE_B]),
        ):
            body = read_view(
                client,
                f"/students/{student_id}/course-subscriptions",
            ).json()
            assert body["subscription_count"] == len(expected_courses)  # noqa: S101
            assert body["courses"] == expected_courses  # noqa: S101
            print(f"OK   {student_id} subscriptions == {expected_courses}")

        # course_catalogue: final state after the capacity change and re-subscription
        catalogue = read_view(client, "/course-catalogue").json()["courses"]
        by_id = {entry["course_id"]: entry for entry in catalogue}
        assert by_id[COURSE_A]["capacity"] == 2  # noqa: S101
        assert by_id[COURSE_A]["number_of_subscriptions"] == 2  # noqa: S101
        assert by_id[COURSE_B]["capacity"] == 10  # noqa: S101
        assert by_id[COURSE_B]["number_of_subscriptions"] == 1  # noqa: S101
        print(
            "OK   final catalogue matches expected capacities and subscription counts",
        )

        # request-schema validation
        expect(
            client.post(
                "/courses/register",
                json={"course_id": "EM-0000-001", "title": "No capacity"},
            ),
            422,
            label="register course missing capacity field",
        )
        expect(
            client.post(f"/students/{STUDENT_1}/subscribe-to-course", json={}),
            422,
            label="subscribe missing course_id field",
        )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} scenario assertion(s) failed.")
        return 1
    print("\nAll scenario assertions passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError as error:
        print(f"FAIL {error}")
        sys.exit(1)
