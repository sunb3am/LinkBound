import pytest

from app.queue_schedule import distribute_due_times


def test_daily_chunks_follow_local_clock_across_dst_boundary():
    due = distribute_due_times("2026-03-07T09:00", "America/Los_Angeles", 2, 5)
    assert due == [
        "2026-03-07T17:00:00+00:00",
        "2026-03-07T17:00:00+00:00",
        "2026-03-08T16:00:00+00:00",
        "2026-03-08T16:00:00+00:00",
        "2026-03-09T16:00:00+00:00",
    ]


@pytest.mark.parametrize("start", ["2026-03-08T02:30", "2026-11-01T01:30"])
def test_rejects_nonexistent_or_ambiguous_local_time(start):
    with pytest.raises(ValueError, match="[Ll]ocal time"):
        distribute_due_times(start, "America/Los_Angeles", 1, 1)


def test_rejects_invalid_chunk_and_timezone():
    with pytest.raises(ValueError, match="daily_chunk"):
        distribute_due_times("2026-03-07T09:00", "America/Los_Angeles", 101, 1)
    with pytest.raises(ValueError, match="IANA timezone"):
        distribute_due_times("2026-03-07T09:00", "Mars/West", 1, 1)
