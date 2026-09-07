"""Handlers for Cloud Marketplace Pub/Sub lifecycle events.

Plain Django, no Temporal. The consumer activity is a thin caller.

Two rules hold everywhere:

1. Never trust the message body. It carries an event type and an id, nothing
   more, so every handler re-fetches current state from the Procurement API.
   A redelivered message from an hour ago then still produces today's answer.

2. Only terminal events change access. `*_REQUESTED` and `*_PENDING_*` are
   notifications: the customer has asked for something that has not happened
   yet. Acting on them revokes access people have paid for.
"""

import structlog
from django.db import transaction

from accounts.models.gcp_marketplace import (
    GCPMarketplaceAccount,
    GCPMarketplaceEntitlement,
    GCPMarketplaceProcessedEvent,
)
from accounts.services.gcp_procurement import gcp_procurement, resolve_plan

logger = structlog.get_logger(__name__)

try:
    from ee.usage.models.usage import (
        BillingMethodChoices,
        OrganizationSubscription,
        PlanChoices,
    )
except ImportError:
    BillingMethodChoices = None
    OrganizationSubscription = None
    PlanChoices = None


def _parse_time(value):
    if not value:
        return None
    from django.utils.dateparse import parse_datetime

    return parse_datetime(value)


def _subject_id(payload: dict) -> str:
    for key in ("entitlement", "account"):
        subject = payload.get(key) or {}
        if subject.get("id"):
            return subject["id"]
    return ""


def _refresh_plan_caches(organization_id) -> None:
    """Every write to OrganizationSubscription.plan owes these two calls.

    Without them the resolver serves the previous plan for up to the cache TTL,
    so a customer who just paid keeps getting 402s until it expires.

    Deferred to commit because process_event runs handlers inside a transaction.
    publish_quotas_for_org reads the plan back and caches what it finds, so
    running it early would repopulate the cache from a write that may still roll
    back, and leave the stale value there for the rest of the TTL.
    """

    def _refresh():
        from ee.usage.services.entitlements import invalidate_plan_caches
        from ee.usage.services.metering import publish_quotas_for_org

        invalidate_plan_caches(str(organization_id))
        publish_quotas_for_org(str(organization_id))

    transaction.on_commit(_refresh)


def _apply_plan(entitlement_row: GCPMarketplaceEntitlement) -> None:
    """Set the organization's plan from the entitlement's marketplace plan."""
    if OrganizationSubscription is None or not entitlement_row.organization_id:
        return

    try:
        plan, interval = resolve_plan(entitlement_row.plan_id)
    except ValueError:
        # Raising leaves the message unacked and Google redelivers a plan we
        # still cannot map, for ever. The mapping needs a human.
        logger.error(
            "gcp_marketplace_unmapped_plan",
            entitlement_id=entitlement_row.entitlement_id,
            plan_id=entitlement_row.plan_id,
        )
        return

    OrganizationSubscription.objects.filter(
        organization_id=entitlement_row.organization_id
    ).update(
        plan=plan,
        billing_interval=interval,
        billing_method=BillingMethodChoices.GCP_MARKETPLACE,
    )
    _refresh_plan_caches(entitlement_row.organization_id)
    logger.info(
        "gcp_marketplace_plan_applied",
        entitlement_id=entitlement_row.entitlement_id,
        plan=plan,
        interval=interval,
    )


def _downgrade_org_to_free(organization_id) -> None:
    """Drop to free and hand billing back. Customer data is untouched."""
    if OrganizationSubscription is None or not organization_id:
        return

    OrganizationSubscription.objects.filter(organization_id=organization_id).update(
        plan=PlanChoices.FREE,
        billing_method=BillingMethodChoices.CARD,
    )
    _refresh_plan_caches(organization_id)


def _downgrade_to_free(entitlement_row: GCPMarketplaceEntitlement) -> None:
    _downgrade_org_to_free(entitlement_row.organization_id)
    logger.info(
        "gcp_marketplace_downgraded_to_free",
        entitlement_id=entitlement_row.entitlement_id,
    )


def sync_entitlement(entitlement_id: str) -> GCPMarketplaceEntitlement | None:
    """Fetch an entitlement from Google and mirror it locally.

    Returns None when the incoming state is older than what we hold: Pub/Sub
    gives no ordering, so a delayed event can otherwise overwrite newer state.
    """
    remote = gcp_procurement.get_entitlement(entitlement_id)

    update_time = _parse_time(remote.get("updateTime"))
    account_id = gcp_procurement.bare_id(remote.get("account", ""))

    existing = GCPMarketplaceEntitlement.objects.filter(
        entitlement_id=entitlement_id
    ).first()
    if (
        existing
        and existing.google_update_time
        and update_time
        and existing.google_update_time > update_time
    ):
        logger.info(
            "gcp_marketplace_stale_event_ignored", entitlement_id=entitlement_id
        )
        return None

    account = GCPMarketplaceAccount.objects.filter(
        procurement_account_id=account_id
    ).first()

    defaults = {
        "account": account,
        "organization": account.organization if account else None,
        "plan_id": remote.get("plan", ""),
        "new_pending_plan": remote.get("newPendingPlan", "") or "",
        "status": remote.get("state", ""),
        "usage_reporting_id": remote.get("usageReportingId", "") or "",
        "effective_at": _parse_time(remote.get("createTime")),
        "expires_at": _parse_time(remote.get("subscriptionEndTime")),
        "offer": remote.get("offer", "") or "",
        "offer_end_time": _parse_time(remote.get("offerEndTime")),
        "cancellation_reason": remote.get("cancellationReason", "") or "",
        "google_update_time": update_time,
        "raw_payload": remote,
    }

    row, _ = GCPMarketplaceEntitlement.objects.update_or_create(
        entitlement_id=entitlement_id, defaults=defaults
    )
    return row


# ── handlers ──────────────────────────────────────────────────────────────


def handle_account_active(payload: dict) -> None:
    account_id = _subject_id(payload)
    remote = gcp_procurement.get_account(account_id)
    GCPMarketplaceAccount.objects.filter(procurement_account_id=account_id).update(
        state=remote.get("state", ""), raw_payload=remote
    )


def handle_account_deleted(payload: dict) -> None:
    """Unlink, never delete. This ends a billing relationship, not a customer."""
    account_id = _subject_id(payload)
    account = GCPMarketplaceAccount.objects.filter(
        procurement_account_id=account_id
    ).first()
    if account is None:
        return

    # Handed back before the link goes, or the org keeps a Marketplace billing
    # method with no Marketplace behind it and nothing bills it at all.
    _downgrade_org_to_free(account.organization_id)

    account.organization = None
    account.save(update_fields=["organization", "updated_at"])
    logger.info("gcp_marketplace_account_deleted", account_id=account_id)


def handle_entitlement_creation_requested(payload: dict) -> None:
    """Approve, or hold it if sign-up has not completed yet.

    An entitlement cannot be approved before its account is approved, and the
    account is only approved once the customer has a User with us. Anything that
    arrives first is stored and approve_pending_entitlements picks it up.
    """
    row = sync_entitlement(_subject_id(payload))
    if row is None:
        return

    if not row.account_id or not row.account.approved_at:
        logger.info(
            "gcp_marketplace_entitlement_held_for_signup",
            entitlement_id=row.entitlement_id,
        )
        return

    gcp_procurement.approve_entitlement(row.entitlement_id)


def handle_entitlement_active(payload: dict) -> None:
    """Billing starts here. usage_reporting_id is captured by sync_entitlement."""
    row = sync_entitlement(_subject_id(payload))
    if row is None:
        return
    _apply_plan(row)

    if not row.usage_reporting_id:
        logger.warning(
            "gcp_marketplace_missing_usage_reporting_id",
            entitlement_id=row.entitlement_id,
        )


def handle_plan_change_requested(payload: dict) -> None:
    """Approve the pending plan. Access does not change until PLAN_CHANGED."""
    row = sync_entitlement(_subject_id(payload))
    if row is None:
        return
    if not row.new_pending_plan:
        logger.warning(
            "gcp_marketplace_plan_change_without_pending_plan",
            entitlement_id=row.entitlement_id,
        )
        return
    gcp_procurement.approve_plan_change(row.entitlement_id, row.new_pending_plan)


def handle_plan_changed(payload: dict) -> None:
    """The change has taken effect. Only now does the plan move."""
    row = sync_entitlement(_subject_id(payload))
    if row is None:
        return
    _apply_plan(row)


def handle_entitlement_cancelled(payload: dict) -> None:
    row = sync_entitlement(_subject_id(payload))
    if row is None:
        return

    # Access first: a billing failure must never leave a cancelled customer on
    # a paid plan. The final report goes last so nothing after it can roll the
    # transaction back and lose the checkpoints it just wrote.
    _downgrade_to_free(row)

    from accounts.gcp_marketplace_usage import report_final_window

    try:
        report_final_window(row)
    except Exception:
        # Under an hour of usage, and the entitlement is gone from the hourly
        # sweep, so there is no retry. Logged rather than raised: redelivering
        # this event would re-run the downgrade for nothing.
        logger.exception(
            "gcp_marketplace_final_usage_report_failed",
            entitlement_id=row.entitlement_id,
        )


def handle_entitlement_renewed(payload: dict) -> None:
    """Moves the billing period boundary. Nothing else reports that it moved."""
    row = sync_entitlement(_subject_id(payload))
    if row is None:
        return
    _apply_plan(row)


def handle_offer_accepted(payload: dict) -> None:
    """A private offer, so the entitlement resolves to enterprise.

    Negotiated economics stay on the Marketplace offer. Nothing here reads a
    discount or a committed amount.
    """
    row = sync_entitlement(_subject_id(payload))
    if row is None:
        return
    _apply_plan(row)


def handle_sync_only(payload: dict) -> None:
    """Mirror state without touching access.

    Covers the pending and reverted events. The customer has asked for
    something, or un-asked; either way nothing has taken effect yet.
    """
    sync_entitlement(_subject_id(payload))


HANDLERS = {
    "ACCOUNT_ACTIVE": handle_account_active,
    "ACCOUNT_DELETED": handle_account_deleted,
    "ENTITLEMENT_CREATION_REQUESTED": handle_entitlement_creation_requested,
    "ENTITLEMENT_ACTIVE": handle_entitlement_active,
    "ENTITLEMENT_OFFER_ACCEPTED": handle_offer_accepted,
    "ENTITLEMENT_PLAN_CHANGE_REQUESTED": handle_plan_change_requested,
    "ENTITLEMENT_PLAN_CHANGED": handle_plan_changed,
    "ENTITLEMENT_PLAN_CHANGE_CANCELLED": handle_sync_only,
    "ENTITLEMENT_PENDING_CANCELLATION": handle_sync_only,
    "ENTITLEMENT_CANCELLATION_REVERTED": handle_sync_only,
    "ENTITLEMENT_CANCELLING": handle_sync_only,
    "ENTITLEMENT_CANCELLED": handle_entitlement_cancelled,
    "ENTITLEMENT_RENEWED": handle_entitlement_renewed,
    "ENTITLEMENT_OFFER_ENDED": handle_sync_only,
    "ENTITLEMENT_DELETED": handle_sync_only,
}


def process_event(payload: dict) -> bool:
    """Handle one message exactly once. Returns False if already seen.

    The unique constraint on event_id is what makes this concurrency-safe. The
    ack deadline can expire while a handler is still running, so two workers can
    hold the same message at once; checking in Python first would lose the race.
    """
    event_id = payload.get("eventId")
    event_type = payload.get("eventType", "")

    if not event_id:
        logger.warning("gcp_marketplace_event_without_id", event_type=event_type)
        return False

    handler = HANDLERS.get(event_type)
    if handler is None:
        # Ack rather than raise. An unrecognised type from Google would
        # otherwise redeliver forever and fill the dead-letter queue.
        logger.warning(
            "gcp_marketplace_unknown_event_type",
            event_type=event_type,
            event_id=event_id,
        )
        return False

    with transaction.atomic():
        _, created = GCPMarketplaceProcessedEvent.objects.get_or_create(
            event_id=event_id,
            defaults={
                "event_type": event_type,
                "subject_id": _subject_id(payload),
            },
        )
        if not created:
            logger.info("gcp_marketplace_duplicate_event", event_id=event_id)
            return False

        handler(payload)

    logger.info(
        "gcp_marketplace_event_processed",
        event_type=event_type,
        event_id=event_id,
        subject_id=_subject_id(payload),
    )
    return True
