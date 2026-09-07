"""Service Control client for reporting Marketplace usage to Google.

We send quantities, never money. Google multiplies by the rate configured in
Producer Portal and bills the customer, so whatever is sent here becomes the
invoice with no review step in between.

Google documents no deduplication on operationId, and its own reference
implementation sends a random UUID. The checkpoint table, not this client, is
what prevents a window being reported twice.
"""

import json

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GCPServiceControlNotConfigured(RuntimeError):
    """Raised when the marketplace service name is absent, as on OSS and EE."""


class GCPServiceControlService:
    def __init__(self, service_name: str | None = None):
        self._service_name = service_name or settings.GCP_MARKETPLACE_SERVICE_NAME
        self._client = None

    @property
    def service_name(self) -> str:
        if not self._service_name:
            raise GCPServiceControlNotConfigured(
                "GCP_MARKETPLACE_SERVICE_NAME is not set"
            )
        return self._service_name

    def metric_name(self, metric_id: str) -> str:
        return f"{self.service_name}/{metric_id}"

    def _credentials(self):
        from google.oauth2 import service_account

        sa_json = settings.GCP_MARKETPLACE_SA_JSON
        if sa_json:
            return service_account.Credentials.from_service_account_info(
                json.loads(sa_json), scopes=[CLOUD_SCOPE]
            )

        import google.auth

        credentials, _ = google.auth.default(scopes=[CLOUD_SCOPE])
        return credentials

    @property
    def client(self):
        """Built on first use so importing this module needs no credentials."""
        if self._client is None:
            from googleapiclient.discovery import build

            self._client = build(
                "servicecontrol",
                "v1",
                credentials=self._credentials(),
                cache_discovery=False,
            )
        return self._client

    def build_operation(
        self,
        consumer_id: str,
        operation_id: str,
        start_time: str,
        end_time: str,
        metric_values: dict[str, tuple[float, bool]],
        operation_name: str = "usage_report",
    ) -> dict:
        """Build the operation sent to check and then to report.

        One operation for both calls, so what was checked is what gets billed.
        """
        # Only storage and voice simulation accept floating point. Sending a
        # double where Google expects an int64 is rejected per-operation.
        metric_value_sets = [
            {
                "metricName": self.metric_name(metric_id),
                "metricValues": [
                    (
                        {"doubleValue": float(value)}
                        if is_float
                        else {"int64Value": str(int(value))}
                    )
                ],
            }
            for metric_id, (value, is_float) in metric_values.items()
        ]

        return {
            "operationId": operation_id,
            "operationName": operation_name,
            "consumerId": consumer_id,
            "startTime": start_time,
            "endTime": end_time,
            "metricValueSets": metric_value_sets,
        }

    def check(self, operation: dict) -> list:
        """Whether Google still considers this consumer entitled.

        Returns any check errors. A non-empty list means the report must be
        skipped: it is Google's live answer, independent of whether our Pub/Sub
        consumer has kept the entitlement state current.
        """
        # check rejects userLabels. The caller passes the pre-report operation,
        # but strip defensively so a future caller cannot break the check call.
        body = {k: v for k, v in operation.items() if k != "userLabels"}

        response = (
            self.client.services()
            .check(serviceName=self.service_name, body={"operation": body})
            .execute()
        )

        errors = response.get("checkErrors") or []
        if errors:
            logger.warning(
                "gcp_marketplace_check_errors",
                consumer_id=operation.get("consumerId"),
                operation_id=operation.get("operationId"),
                errors=errors,
            )
        return errors

    def report(
        self, operations: list[dict], user_labels: dict[str, str] | None = None
    ) -> set[str]:
        """Report checked operations. Returns the ids Google rejected.

        An HTTP 200 does not mean the usage was accepted: per-operation failures
        come back in reportErrors. Treating a 200 as success would mark usage
        reported that Google rejected, and it would never be billed.

        Errors are per operation, so one bad metric fails only its own operation
        and the rest still bill. Returning ids rather than a bare list is what
        lets the caller act on that.
        """
        # Forwarded to the customer's Cloud Billing cost-management tools for
        # attribution, and accepted by report but not by check. Omitted entirely
        # when empty rather than sent as {}.
        if user_labels:
            operations = [{**op, "userLabels": user_labels} for op in operations]

        response = (
            self.client.services()
            .report(serviceName=self.service_name, body={"operations": operations})
            .execute()
        )

        errors = response.get("reportErrors") or []
        if errors:
            logger.error(
                "gcp_marketplace_report_errors",
                consumer_id=operations[0].get("consumerId") if operations else None,
                errors=errors,
            )
        return {
            error.get("operationId") for error in errors if error.get("operationId")
        }


gcp_service_control = GCPServiceControlService()
