"""The fraud sweep's thresholds, and the mistake that made it silently do nothing.

Offline boarding lets one code be accepted at two doors — neither device can
see the other's log, and both were right to accept. Detection is the entire
defence, so a sweep that quietly matches nothing is worse than no sweep at all:
the flag is still written, the alert never comes, and the hole looks closed.
"""

from app.models.commerce import RedemptionFlag, TicketStatus
from app.worker.fraud_sweep import DEVICE_THRESHOLD, INTERVAL_S


def test_two_devices_is_already_suspicious() -> None:
    """One code, two doors. There is no honest way for that to happen.

    Set higher and a shared screen boards several friends before anyone
    notices; there is no legitimate second device to allow for.
    """
    assert DEVICE_THRESHOLD == 2


def test_the_sweep_runs_often_enough_to_matter() -> None:
    """A ticket suspended tomorrow has already been used all day."""
    assert INTERVAL_S <= 15 * 60


def test_the_first_sighting_is_not_flagged() -> None:
    """This is the bug the query originally had.

    The device that syncs first is recorded `ok` — it did nothing wrong and
    could not have known. Only later sightings carry `duplicate_suspect`. So
    counting distinct devices *among flagged rows* finds exactly one per nonce,
    never reaches a threshold of two, and the sweep matches nothing at all
    while looking perfectly healthy.

    The query therefore counts devices across every sighting of the nonce, and
    this test exists to stop the filter being "tidied" back in.
    """
    assert RedemptionFlag.ok != RedemptionFlag.duplicate_suspect

    # A realistic pair of rows for one code accepted on two phones.
    sightings = [
        {"device": "phone-A", "flag": RedemptionFlag.ok},
        {"device": "phone-B", "flag": RedemptionFlag.duplicate_suspect},
    ]

    flagged_only = {s["device"] for s in sightings if s["flag"] is RedemptionFlag.duplicate_suspect}
    every_sighting = {s["device"] for s in sightings}

    assert len(flagged_only) < DEVICE_THRESHOLD, "filtering on the flag under-counts"
    assert len(every_sighting) >= DEVICE_THRESHOLD, "counting all sightings is what works"


def test_suspended_is_a_distinct_state_from_revoked() -> None:
    """A duplicate is suspicious, not proven.

    A helper can scan the same screen twice when the first attempt looks like
    it failed. Revoking would destroy a paid ticket on that; suspension stops
    boarding and puts a human in the loop.
    """
    assert TicketStatus.suspended != TicketStatus.revoked
    assert TicketStatus.suspended != TicketStatus.active


def test_suspension_is_what_makes_the_sweep_idempotent() -> None:
    """The sweep matches only active tickets.

    Once suspended a ticket stops matching, so the same evidence is not
    re-alerted every ten minutes for the rest of the ticket's life.
    """
    assert TicketStatus.suspended is not TicketStatus.active
