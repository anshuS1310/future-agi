"""Report Marketplace usage from the existing usage ledger to Google.

An adapter, not a second metering system. Quantities come from UsageSummary,
which is already the source for Stripe reporting, and go out unchanged. No GCP
specific rates, no recalculation, no discounts applied here: a private offer's
economics live on the Marketplace offer and applying them twice would undercharge.

UsageSummary holds a cumulative month-to-date total per organization, dimension
and period. Google wants what was consumed during a window, so the delta is the
cumulative total minus everything already reported for that period.
"""

import re
import uuid as _uuid
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models.gcp_marketplace import (
    IN_SERVICE_STATES,
    GCPMarketplaceEntitlement,
    GCPMarketplaceUsageCheckpoint,
    GCPUsageReportStatus,
)
from accounts.services.gcp_procurement import metric_id_for, resolve_plan
from accounts.services.gcp_service_control import gcp_service_control
from ee.usage.services.config import BillingConfig

logger = structlog.get_logger(__name__)

try:
    from ee.usage.models.usage import UsageSummary
except ImportError:
    UsageSummary = None


def _period_of(moment) -> str:
    return moment.strftime("%Y-%m")


def _period_start(moment):
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _floor_hour(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def _rfc3339(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _org_label(entitlement) -> str:
    """Readable organization tag for the operation name, or the id if unusable."""
    organization = entitlement.organization if entitlement.organization_id else None
    name = (organization.name or "").strip() if organization else ""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:64]
    return slug or str(entitlement.organization_id)


# Reserved Marketplace label key. Free-form keys are accepted but ignored: only
# the reserved ones reach the customer's Cloud Billing cost breakdown. The other
# is cloudmarketplace.googleapis.com/resource_name, unused for now.
CONTAINER_LABEL = "cloudmarketplace.googleapis.com/container_name"


def _cost_attribution(entitlement) -> dict[str, str]:
    """Labels letting the customer attribute this charge inside their own org.

    Only the container is sent. UsageSummary is keyed on organization, so there
    is no finer resource to name until usage is tracked per workspace.
    """
    return {CONTAINER_LABEL: _org_label(entitlement).lower()[:63]}


def _already_reported(entitlement, dimension: str, period_start) -> Decimal:
    """Sum of what we have already sent for this metric in this period.

    Scoped to the period because UsageSummary resets on the 1st. Without that
    scoping the first report of a month computes a negative delta.
    """
    total = GCPMarketplaceUsageCheckpoint.objects.filter(
        entitlement=entitlement,
        metric=dimension,
        report_status=GCPUsageReportStatus.REPORTED,
        window_start__gte=period_start,
    ).aggregate(total=Sum("quantity_reported"))["total"]
    return Decimal(total or 0)


def _window_start_for(entitlement, dimension: str, period_start):
    """Contiguous windows: this one starts where the last one ended.

    Clamped to the period start, or the row would fall outside the range
    _already_reported sums and be reported twice.
    """
    last = (
        GCPMarketplaceUsageCheckpoint.objects.filter(
            entitlement=entitlement,
            metric=dimension,
            report_status=GCPUsageReportStatus.REPORTED,
        )
        .order_by("-window_end")
        .first()
    )
    if last is None:
        return period_start
    return max(last.window_end, period_start)


def _plan_of(entitlement) -> str | None:
    """Internal plan, or None if the portal plan is unmapped. Resolved once."""
    try:
        plan, _interval = resolve_plan(entitlement.plan_id)
    except ValueError:
        logger.error(
            "gcp_marketplace_unmapped_plan",
            entitlement_id=entitlement.entitlement_id,
            plan_id=entitlement.plan_id,
        )
        return None
    return plan


def _free_allowance(dimension: str, plan: str) -> Decimal:
    """Monthly allowance for this dimension, in display units.

    Marketplace plans carry a flat per-unit rate with no allowance configured on
    Google's side, so the allowance has to be applied before reporting. Without
    this a Marketplace customer is charged from the first unit while a direct
    customer on the same plan gets the same allowance free.
    """
    return BillingConfig.get().get_free_allowance(dimension, plan)


def _billable_total(
    entitlement, dimension: str, period: str, plan: str
) -> Decimal | None:
    """Month-to-date usage above the free allowance, or None if not metered."""
    if UsageSummary is None:
        return None

    summary = UsageSummary.objects.filter(
        organization_id=entitlement.organization_id,
        dimension=dimension,
        period=period,
    ).first()
    if summary is None:
        return None

    total = Decimal(summary.total_usage or 0)
    billable = total - _free_allowance(dimension, plan)
    return billable if billable > 0 else Decimal(0)


def _quantity_for(
    entitlement, dimension: str, plan: str, window_start, window_end
) -> Decimal | None:
    """Usage in this window: the rise in the month-to-date total since the last.

    Every dimension is a running total, storage included, so these deltas sum
    to the figure the Stripe invoice run bills from the same ledger.
    """
    period = _period_of(window_end)
    period_start = _period_start(window_end)

    billable_total = _billable_total(entitlement, dimension, period, plan)
    if billable_total is None:
        return None
    if billable_total <= 0:
        return Decimal(0)

    # Subtracting the allowance from the cumulative total handles the crossover
    # on its own: nothing is reported until usage passes it, then only the excess.
    delta = billable_total - _already_reported(entitlement, dimension, period_start)
    return delta if delta > 0 else Decimal(0)


def report_entitlement_usage(
    entitlement: GCPMarketplaceEntitlement,
    _skip_check: bool = False,
    _window_end=None,
) -> int:
    """Report one window of usage for one entitlement. Returns metrics sent.

    `_skip_check` and `_window_end` are private and belong to
    report_final_window alone.
    """
    if not entitlement.usage_reporting_id:
        logger.warning(
            "gcp_marketplace_usage_skipped_no_consumer_id",
            entitlement_id=entitlement.entitlement_id,
        )
        return 0

    plan = _plan_of(entitlement)
    if plan is None:
        return 0

    # The final report passes the cancellation moment, since the part-hour
    # since the last boundary is the whole reason it runs.
    now = _window_end or _floor_hour(timezone.now())
    period_start = _period_start(now)

    # One operation per metric: Google reports errors per operation, so a bad
    # metric id fails alone rather than blocking every dimension.
    operations: list[dict] = []
    by_operation: dict[str, GCPMarketplaceUsageCheckpoint] = {}

    try:
        _collect_operations(
            entitlement, plan, now, period_start, operations, by_operation
        )
    except Exception as exc:
        # Nothing was sent, so anything written is FAILED. Left PENDING it
        # would read as "sent, outcome unknown" and be skipped for ever.
        _mark(list(by_operation.values()), GCPUsageReportStatus.FAILED, str(exc))
        raise

    if not operations:
        return 0

    checkpoints = list(by_operation.values())

    if _skip_check:
        return _send(entitlement, operations, by_operation, [])

    try:
        # Once: check answers for the consumer, identical on every operation.
        check_errors = gcp_service_control.check(operations[0])
    except Exception as exc:
        _mark(checkpoints, GCPUsageReportStatus.FAILED, str(exc))
        raise

    return _send(entitlement, operations, by_operation, check_errors)


def report_final_window(entitlement: GCPMarketplaceEntitlement) -> int:
    """Bill the part-hour between the last report and a cancellation.

    The only caller that may skip check. Google has already cancelled the
    entitlement by the time it tells us, so check would answer "not entitled"
    and we would drop usage the customer really did incur. Nothing else may
    use this: check is what stops a cancelled consumer being billed at all.
    """
    return report_entitlement_usage(
        entitlement, _skip_check=True, _window_end=timezone.now()
    )


def _collect_operations(
    entitlement, plan, now, period_start, operations, by_operation
) -> None:
    """Build one operation per metric, each with a PENDING checkpoint written.

    Accumulators are arguments, not return values, so a caller can still mark
    what was written if this raises partway through.
    """
    for dimension in settings.GCP_MARKETPLACE_DIMENSIONS:
        metric_id = metric_id_for(entitlement.plan_id, dimension)
        if not metric_id:
            logger.warning(
                "gcp_marketplace_no_metric_for_plan",
                plan_id=entitlement.plan_id,
                dimension=dimension,
            )
            continue
        window_start = _window_start_for(entitlement, dimension, period_start)
        if window_start >= now:
            continue

        quantity = _quantity_for(entitlement, dimension, plan, window_start, now)
        if quantity is None or quantity <= 0:
            continue

        is_float = dimension in settings.GCP_MARKETPLACE_FLOAT_DIMENSIONS
        if not is_float:
            # Google takes an int64, so record what goes out. Storing the
            # fraction would count it reported and drop it.
            quantity = quantity.to_integral_value(rounding=ROUND_FLOOR)
            if quantity <= 0:
                continue

        existing = GCPMarketplaceUsageCheckpoint.objects.filter(
            entitlement=entitlement, metric=dimension, window_start=window_start
        ).first()
        if existing and existing.report_status == GCPUsageReportStatus.PENDING:
            # Outcome unknown. Resending risks double billing, so leave it
            # for reconcile_usage to surface.
            logger.warning(
                "gcp_marketplace_usage_window_unresolved",
                entitlement_id=entitlement.entitlement_id,
                metric=dimension,
            )
            continue

        operation_id = str(_uuid.uuid4())

        # Reused, not inserted: the window is unique per metric, so a failed
        # attempt would collide here every hour after.
        checkpoint, _ = GCPMarketplaceUsageCheckpoint.objects.update_or_create(
            entitlement=entitlement,
            metric=dimension,
            window_start=window_start,
            defaults={
                "organization_id": entitlement.organization_id,
                "window_end": now,
                "quantity_reported": quantity,
                "operation_id": operation_id,
                "report_status": GCPUsageReportStatus.PENDING,
                "reported_at": None,
                "error_detail": "",
            },
        )
        by_operation[operation_id] = checkpoint
        operations.append(
            gcp_service_control.build_operation(
                consumer_id=entitlement.usage_reporting_id,
                operation_id=operation_id,
                start_time=_rfc3339(window_start),
                end_time=_rfc3339(now),
                metric_values={metric_id: (float(quantity), is_float)},
                operation_name=(
                    f"usage_report_{_org_label(entitlement)}_{dimension}"
                    f"_{_rfc3339(window_start)}_{_rfc3339(now)}"
                ),
            )
        )


def _send(entitlement, operations, by_operation, check_errors) -> int:
    """Report the checked operations and record each outcome on its own row."""
    checkpoints = list(by_operation.values())

    if check_errors:
        # Nothing is lost by stopping here. The delta is computed from REPORTED
        # checkpoints only, so this window folds into the next successful one.
        _mark(checkpoints, GCPUsageReportStatus.FAILED, str(check_errors))
        logger.warning(
            "gcp_marketplace_usage_skipped_check_failed",
            entitlement_id=entitlement.entitlement_id,
        )
        return 0

    try:
        rejected = gcp_service_control.report(
            operations, user_labels=_cost_attribution(entitlement)
        )
    except Exception as exc:
        _mark(checkpoints, GCPUsageReportStatus.FAILED, str(exc))
        raise

    failed = [by_operation[oid] for oid in rejected if oid in by_operation]
    reported = [c for oid, c in by_operation.items() if oid not in rejected]

    if failed:
        _mark(failed, GCPUsageReportStatus.FAILED, "rejected by Service Control")
    if reported:
        _mark(reported, GCPUsageReportStatus.REPORTED, "")

    logger.info(
        "gcp_marketplace_usage_reported",
        entitlement_id=entitlement.entitlement_id,
        metrics=len(reported),
        rejected=len(failed),
    )
    return len(reported)


def _mark(checkpoints, status, error_detail) -> None:
    reported_at = timezone.now() if status == GCPUsageReportStatus.REPORTED else None
    with transaction.atomic():
        for checkpoint in checkpoints:
            checkpoint.report_status = status
            checkpoint.reported_at = reported_at
            checkpoint.error_detail = error_detail[:2000]
            checkpoint.save(
                update_fields=[
                    "report_status",
                    "reported_at",
                    "error_detail",
                    "updated_at",
                ]
            )


def report_all_usage() -> dict:
    """Report the current window for every in-service entitlement."""
    active = (
        GCPMarketplaceEntitlement.objects.filter(
            status__in=IN_SERVICE_STATES,
            organization__isnull=False,
        )
        .select_related("organization")
        .exclude(usage_reporting_id="")
    )

    entitlements = 0
    metrics = 0
    failures = 0

    for entitlement in active.iterator(chunk_size=200):
        try:
            metrics += report_entitlement_usage(entitlement)
            entitlements += 1
        except Exception:
            failures += 1
            logger.exception(
                "gcp_marketplace_usage_report_failed",
                entitlement_id=entitlement.entitlement_id,
            )

    return {
        "entitlements": entitlements,
        "metrics": metrics,
        "failures": failures,
    }


def reconcile_usage(period: str | None = None) -> list[dict]:
    """Compare the ledger against what we recorded as reported.

    Under-reporting is silent: no customer complains about being charged too
    little, so nothing else surfaces it. Over-reporting reaches them as a wrong
    invoice. Neither shows up without this comparison.
    """
    if UsageSummary is None:
        return []

    now = timezone.now()
    period = period or _period_of(now)
    period_start = _period_start(now)

    discrepancies = []
    active = GCPMarketplaceEntitlement.objects.filter(
        organization__isnull=False
    ).exclude(usage_reporting_id="")

    for entitlement in active.iterator(chunk_size=200):
        plan = _plan_of(entitlement)
        if plan is None:
            continue

        for dimension in settings.GCP_MARKETPLACE_DIMENSIONS:
            # After the allowance, because that is what we report: the raw
            # total would flag every org by its own allowance. None means no
            # usage row, so zero, not skip -- skipping hides an over-report.
            ledger_total = _billable_total(entitlement, dimension, period, plan)
            ledger_total = (ledger_total or Decimal(0)).to_integral_value(
                rounding=ROUND_FLOOR
            )

            reported = GCPMarketplaceUsageCheckpoint.objects.filter(
                entitlement=entitlement,
                metric=dimension,
                report_status=GCPUsageReportStatus.REPORTED,
                window_start__gte=period_start,
            ).aggregate(total=Sum("quantity_reported"))["total"]
            reported_total = Decimal(reported or 0)

            if ledger_total == reported_total:
                continue

            discrepancy = {
                "entitlement_id": entitlement.entitlement_id,
                "organization_id": str(entitlement.organization_id),
                "metric": dimension,
                "period": period,
                "ledger": str(ledger_total),
                "reported": str(reported_total),
                "difference": str(ledger_total - reported_total),
            }
            discrepancies.append(discrepancy)
            logger.warning("gcp_marketplace_usage_discrepancy", **discrepancy)

    stale = GCPMarketplaceUsageCheckpoint.objects.filter(
        report_status=GCPUsageReportStatus.PENDING,
        created_at__lt=now - timedelta(hours=6),
    ).count()
    if stale:
        # Pending means we called Google and never learned the outcome. Retrying
        # risks double billing and skipping risks losing revenue, so these are
        # surfaced for a human rather than resolved automatically.
        logger.warning("gcp_marketplace_usage_checkpoints_stuck_pending", count=stale)

    return discrepancies
