"""Who may open the admin Custom Tools pages, and in which mode.

The page/template rendering and the API boundary are covered in the
future-agi/ee repo (``ee/cloud/tests/test_admin_custom_tools_access.py``);
this pins the role → mode decision that lives in this repo.
"""

import uuid

import pytest
from django.test import RequestFactory

from accounts.models.user import User
from ee.usage.admin import (
    _custom_tools_read_only,
    custom_pricing_view,
    generate_invoice_view,
)


def _request(user):
    request = RequestFactory().get("/admin/usage/")
    request.user = user
    return request


def _user(organization, label, **flags):
    return User.objects.create(
        email=f"{label}-{uuid.uuid4().hex[:8]}@futureagi.com",
        name=label.title(),
        organization=organization,
        **flags,
    )


@pytest.mark.django_db
class TestCustomToolsReadOnlyMode:
    def test_staff_gets_read_only(self, organization):
        staff = _user(organization, "staff", is_staff=True, is_superuser=False)
        assert _custom_tools_read_only(_request(staff)) is True

    def test_superuser_gets_full_access(self, organization):
        root = _user(organization, "root", is_staff=True, is_superuser=True)
        assert _custom_tools_read_only(_request(root)) is False

    def test_non_staff_is_refused(self, organization):
        customer = _user(organization, "customer", is_staff=False)
        assert _custom_tools_read_only(_request(customer)) is None

    def test_inactive_staff_is_refused(self, organization):
        gone = _user(organization, "gone", is_staff=True, is_active=False)
        assert _custom_tools_read_only(_request(gone)) is None

    def test_pages_return_403_for_refused_users(self, organization):
        customer = _user(organization, "customer", is_staff=False)
        assert custom_pricing_view(_request(customer)).status_code == 403
        assert generate_invoice_view(_request(customer)).status_code == 403
