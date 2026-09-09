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

    assert _spend(job)["total_usd"] == 0.8123
    assert _spend(job)["unpriced_turns"] == 2
    assert _spend(job)["attempts"]["1"]["stages"] == [
        {"stage": "understand-agent", "usd": 0.8123, "turns": 9}
    ]
    assert job.saved


def test_a_later_read_never_lowers_the_bill():
    """A half-written or reset ledger must not erase what a previous poll already saw."""
    job = _job(
        {
            "harness_spend": {
                "total_usd": 1.5,
                "unpriced_turns": 0,
                "attempts": {"1": {"total_usd": 1.5, "unpriced_turns": 0, "stages": []}},
            }
        }
    )
    _record_harness_spend(job, {"total_usd": 0.2, "stages": []})

    assert _spend(job)["total_usd"] == 1.5
    assert not job.saved


def test_a_growing_total_replaces_the_earlier_one():
    job = _job(
        {
            "harness_spend": {
                "total_usd": 0.5,
                "unpriced_turns": 0,
                "attempts": {"1": {"total_usd": 0.5, "unpriced_turns": 0, "stages": []}},
            }
        }
    )
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
        attempt_number = 1

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
        attempt_number = 1

    gateway._read_harness_spend(_Attempt(), _Sandbox())


def test_a_retry_adds_to_the_bill_instead_of_replacing_it():
    """A retry runs in a NEW sandbox whose ledger starts at zero, so attempts must be summed."""
    job = _job()
    _record_harness_spend(job, {"total_usd": 1.2, "unpriced_turns": 1, "stages": []}, 1)
    _record_harness_spend(job, {"total_usd": 0.8, "unpriced_turns": 0, "stages": []}, 2)

    spend = _spend(job)
    assert spend["total_usd"] == 2.0
    assert spend["unpriced_turns"] == 1
    assert sorted(spend["attempts"]) == ["1", "2"]


def test_one_attempt_growing_does_not_disturb_another():
    job = _job()
    _record_harness_spend(job, {"total_usd": 1.0, "stages": []}, 1)
    _record_harness_spend(job, {"total_usd": 0.3, "stages": []}, 2)
    _record_harness_spend(job, {"total_usd": 0.9, "stages": []}, 2)

    spend = _spend(job)
    assert spend["attempts"]["1"]["total_usd"] == 1.0
    assert spend["attempts"]["2"]["total_usd"] == 0.9
    assert spend["total_usd"] == 1.9


def test_the_ledger_is_read_before_the_only_delete_that_exists(monkeypatch):
    """One delete site, and the read must come first: after it, there is nothing left to ask.

    Pins the ordering rather than the wording, so a second deletion path or a reordered one fails
    here instead of silently losing a bill.
    """
    import sys
    import types

    from simulate.services import hosted_harness_gateway as gateway

    # The method imports daytona at call time, and the SDK is not a test dependency.
    fake = types.ModuleType("daytona")
    fake.DaytonaNotFoundError = type("DaytonaNotFoundError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "daytona", fake)

    order = []

    class _Client:
        def get(self, ref):
            return "sandbox"

        def delete(self, sandbox, **kwargs):
            order.append("delete")

    class _Attempt:
        id = "attempt-3"
        job_id = "job-3"
        attempt_number = 1
        provider_ref = "ref-3"

    monkeypatch.setattr(
        gateway, "_read_harness_spend", lambda *_: order.append("read_spend")
    )
    monkeypatch.setattr(gateway, "record_cleanup", lambda *a, **k: None)

    driver = gateway.DaytonaHostedGateway.__new__(gateway.DaytonaHostedGateway)
    driver.client = _Client()
    driver._delete_and_record(_Attempt())

    assert order == ["read_spend", "delete"]


def test_the_authoring_archive_does_not_carry_a_previous_run_s_bill():
    """A reuse run restores the authoring archive instead of authoring, so a bill left inside it is
    read back as though this run had spent it.

    Measured before this: three runs reported total_usd 3.533797 with identical token counts to the
    last token (build-environment 636406, understand-agent 1323174, validate-source-data 2226074,
    write-scenarios 409913), and the restored files were stamped 19:12 and 19:17 UTC, which is the
    FIRST run's authoring window. Every later run was reporting money it never spent.
    """
    import inspect

    from simulate.services import hosted_harness_gateway as gateway

    source = inspect.getsource(gateway)
    pack = source[source.index("tar -czf /tmp/authoring.tar.gz") :][:400]

    assert "--exclude=./cost.json" in pack, (
        "cost.json must be kept out of the reusable archive, or every reuse re-reports the "
        "first run's authoring bill"
    )
    # The rest of the directory is still packed whole: the guest decides what a saved world is.
    assert "--exclude=./environment-bundle" in pack
    assert "--exclude=__pycache__" in pack
