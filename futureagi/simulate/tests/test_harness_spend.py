"""The harness's own cost, as the platform records it from the guest's ledger."""

from __future__ import annotations

from simulate.models import HostedHarnessJob
from simulate.services.hosted_harness_gateway import _record_harness_spend


class _Job(HostedHarnessJob):
    """A job that records what it would have written instead of writing it."""

    class Meta:
        proxy = True

    def save(self, *args, **kwargs):  # noqa: D102 - persistence is not what these test
        self.saved = True


def _job(metadata=None):
    job = _Job(payload={"metadata": metadata or {}})
    job.saved = False
    return job


def _spend(job):
    return ((job.payload or {}).get("metadata") or {}).get("harness_spend")


def test_the_total_and_its_stages_land_on_the_job():
    job = _job()
    _record_harness_spend(
        job,
        {
            "total_usd": 0.8123,
            "unpriced_turns": 2,
            "stages": [{"stage": "understand-agent", "usd": 0.8123, "turns": 9}],
        },
    )

    assert _spend(job) == {
        "total_usd": 0.8123,
        "unpriced_turns": 2,
        "stages": [{"stage": "understand-agent", "usd": 0.8123, "turns": 9}],
    }
    assert job.saved


def test_a_later_read_never_lowers_the_bill():
    """A half-written or reset ledger must not erase what a previous poll already saw."""
    job = _job({"harness_spend": {"total_usd": 1.5, "unpriced_turns": 0, "stages": []}})
    _record_harness_spend(job, {"total_usd": 0.2, "stages": []})

    assert _spend(job)["total_usd"] == 1.5
    assert not job.saved


def test_a_growing_total_replaces_the_earlier_one():
    job = _job({"harness_spend": {"total_usd": 0.5, "unpriced_turns": 0, "stages": []}})
    _record_harness_spend(job, {"total_usd": 0.9, "stages": [{"stage": "x", "usd": 0.9}]})

    assert _spend(job)["total_usd"] == 0.9
    assert job.saved


def test_nothing_readable_is_left_alone():
    """An absent ledger is the ordinary case early in a run, not an error."""
    for unreadable in (None, "", [], {"total_usd": "not a number"}):
        job = _job()
        _record_harness_spend(job, unreadable)
        assert _spend(job) is None
        assert not job.saved


def test_a_dying_sandbox_is_read_before_it_is_deleted(monkeypatch):
    """The pull has to happen while the sandbox still exists, whatever ended the attempt."""
    from simulate.services import hosted_harness_gateway as gateway

    job = _job()
    read = {}

    class _Files:
        def download_file(self, path):
            read["path"] = path
            return b'{"total_usd": 0.42, "unpriced_turns": 0, "stages": []}'

    class _Sandbox:
        fs = _Files()

    class _Attempt:
        id = "attempt-1"
        job_id = "job-1"

    monkeypatch.setattr(
        gateway.HostedHarnessJob.no_workspace_objects, "get", lambda **_: job
    )
    gateway._read_harness_spend(_Attempt(), _Sandbox())

    assert read["path"] == "/work/authoring/cost.json"
    assert _spend(job)["total_usd"] == 0.42


def test_a_sandbox_that_cannot_be_read_never_blocks_its_own_deletion(monkeypatch):
    """A leaked sandbox costs more than the figure it was holding."""
    from simulate.services import hosted_harness_gateway as gateway

    class _Files:
        def download_file(self, path):
            raise RuntimeError("sandbox gone")

    class _Sandbox:
        fs = _Files()

    class _Attempt:
        id = "attempt-2"
        job_id = "job-2"

    gateway._read_harness_spend(_Attempt(), _Sandbox())
