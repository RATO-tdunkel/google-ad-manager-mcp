#!/usr/bin/env python3
"""Smoke-test the tools against a live GAM test network.

The unit tests mock the SOAP layer, so they cannot catch API drift after a
version bump, nor zeep objects being treated as dicts. This script exercises
the read and write paths for real.

It refuses to run against anything but a network GAM reports as a test network,
and it archives everything it creates.

GAM offers no way to remove a creative - there is no delete action, and
DeactivateCreatives needs the ACTIVATE_AND_DEACTIVATE_CREATIVES network feature,
which is not enabled everywhere. Companies cannot be removed at all. So this
script creates no companies and reuses its own creative across runs, leaving at
most one behind however often it is run.

Usage:
    GAM_CREDENTIALS_PATH=/path/to/sa.json \
    GAM_TEST_NETWORK_CODE=87654321098 \
    python scripts/verify_live.py

Optional:
    GAM_API_VERSION           pin a specific API version for this run
    GAM_TEST_ADVERTISER_ID    skip advertiser discovery
    GAM_TEST_AD_UNIT_ID       skip ad unit discovery
"""

import datetime
import os
import sys
import warnings

warnings.filterwarnings("ignore")

from gam_mcp.client import get_gam_client, init_gam_client  # noqa: E402
from gam_mcp.tools import creatives, line_items, orders, verification  # noqa: E402
from gam_mcp.utils import safe_get  # noqa: E402

TAG = "MCP-VERIFY " + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
failures = []


def step(label, fn, expect_error=False):
    """Run one tool call and record whether it behaved as expected."""
    try:
        result = fn()
    except Exception as e:
        failures.append(f"{label}: raised {type(e).__name__}")
        print(f"  {label:38s} RAISED {type(e).__name__}: {str(e)[:90]}")
        return None

    is_error = isinstance(result, dict) and "error" in result
    if is_error and not expect_error:
        failures.append(f"{label}: {result['error']}")
        print(f"  {label:38s} ERROR {str(result['error'])[:90]}")
    elif is_error:
        print(f"  {label:38s} ok (error dict, as expected)")
    elif expect_error:
        failures.append(f"{label}: expected an error, got a result")
        print(f"  {label:38s} UNEXPECTED SUCCESS")
    else:
        preview = str({k: v for k, v in list(result.items())[:3]})[:95]
        print(f"  {label:38s} ok  {preview}")
    return result


CREATIVE_NAME = "MCP-VERIFY reusable creative"


def first_id(client, service, method, statement_limit=1):
    """Return the id of the first result from a paged getter."""
    svc = client.get_service(service)
    page = getattr(svc, method)(client.create_statement().Limit(statement_limit).ToStatement())
    results = safe_get(page, "results") or []
    return safe_get(results[0], "id") if results else None


def reusable_creative_id(client):
    """Find this script's creative from an earlier run, if it left one."""
    statement = client.create_statement().Where("name = :name").WithBindVariable(
        "name", CREATIVE_NAME).Limit(1)
    page = client.get_service("CreativeService").getCreativesByStatement(statement.ToStatement())
    results = safe_get(page, "results") or []
    return safe_get(results[0], "id") if results else None


def main():
    credentials = os.environ.get("GAM_CREDENTIALS_PATH")
    network_code = os.environ.get("GAM_TEST_NETWORK_CODE")
    if not credentials or not network_code:
        sys.exit("GAM_CREDENTIALS_PATH and GAM_TEST_NETWORK_CODE are required")

    # The allowlist holds only the test network, so no call can reach production.
    init_gam_client(credentials, network_code,
                    api_version=os.environ.get("GAM_API_VERSION") or None)
    client = get_gam_client()

    network = client.get_service("NetworkService").getCurrentNetwork()
    if safe_get(network, "isTest") is not True:
        sys.exit(f"refusing to write: {safe_get(network, 'displayName')!r} is not a test network")

    print(f"network:  {safe_get(network, 'displayName')!r} ({network_code})")
    print(f"api:      {client.api_version}")
    print(f"settings: {client.get_network_settings()}\n")

    advertiser_id = os.environ.get("GAM_TEST_ADVERTISER_ID") or first_id(
        client, "CompanyService", "getCompaniesByStatement")
    ad_unit_id = os.environ.get("GAM_TEST_AD_UNIT_ID") or first_id(
        client, "InventoryService", "getAdUnitsByStatement")
    if not advertiser_id or not ad_unit_id:
        sys.exit("test network needs at least one advertiser and one ad unit")
    print(f"using advertiser {advertiser_id}, ad unit {ad_unit_id}\n")

    print("--- read paths ---")
    step("list_delivering_orders", orders.list_delivering_orders)

    print("\n--- write paths ---")
    order = step("create_order", lambda: orders.create_order(
        order_name=f"{TAG} order", advertiser_id=int(advertiser_id)))
    if not order or "error" in order:
        return finish()

    end = datetime.date.today() + datetime.timedelta(days=7)
    # No currency_code: it must fall back to the network's own currency.
    line_item = step("create_line_item", lambda: line_items.create_line_item(
        order_id=order["id"], name=f"{TAG} line item",
        end_year=end.year, end_month=end.month, end_day=end.day,
        target_ad_unit_id=str(ad_unit_id), goal_impressions=1000,
        cost_per_unit_micro=1000000))
    if not line_item or "error" in line_item:
        return finish()

    line_item_id = line_item["id"]
    step("get_line_item", lambda: line_items.get_line_item(line_item_id=line_item_id))
    step("update_line_item", lambda: line_items.update_line_item(
        line_item_id=line_item_id, name=f"{TAG} line item v2", goal_impressions=2500))
    step("verify_line_item_setup",
         lambda: verification.verify_line_item_setup(line_item_id=line_item_id))
    step("verify_order_setup", lambda: verification.verify_order_setup(order_id=order["id"]))
    step("check_line_item_delivery_status",
         lambda: verification.check_line_item_delivery_status(line_item_id=line_item_id))

    # Needs a role that may approve orders; a Trafficker role cannot.
    step("approve_order", lambda: orders.approve_order(order_id=order["id"]))

    # A line item without creatives cannot be resumed. GAM refuses this, and the
    # tool must report that as an error dict rather than let the fault escape.
    step("resume_line_item (refused)",
         lambda: line_items.resume_line_item(line_item_id=line_item_id), expect_error=True)

    # Reuse the creative from an earlier run; only create one the first time.
    creative_id = reusable_creative_id(client)
    if creative_id:
        print(f"  {'reusing creative':38s} ok  id={creative_id}")
        step("get_creative", lambda: creatives.get_creative(creative_id=creative_id))
    else:
        created = step("create_third_party_creative", lambda: creatives.create_third_party_creative(
            advertiser_id=int(advertiser_id), name=CREATIVE_NAME, width=300, height=250,
            snippet='<div style="width:300px;height:250px">MCP verify</div>'))
        creative_id = created["id"] if created and "error" not in created else None

    if creative_id:
        step("associate_creative_with_line_item",
             lambda: creatives.associate_creative_with_line_item(
                 creative_id=creative_id, line_item_id=line_item_id))
        step("list_creatives_by_line_item",
             lambda: creatives.list_creatives_by_line_item(line_item_id=line_item_id))

    print("\n--- cleanup ---")
    step("archive_line_item", lambda: line_items.archive_line_item(line_item_id=line_item_id))

    def archive_order():
        statement = client.create_statement().Where("id = :id").WithBindVariable("id", order["id"])
        changes = safe_get(client.get_service("OrderService").performOrderAction(
            {"xsi_type": "ArchiveOrders"}, statement.ToStatement()), "numChanges")
        return {"numChanges": changes}

    step("archive order", archive_order)

    # The association can be deleted even though the creative cannot.
    if creative_id:
        def delete_association():
            statement = client.create_statement().Where(
                "lineItemId = :id").WithBindVariable("id", line_item_id)
            changes = safe_get(client.get_service("LineItemCreativeAssociationService")
                               .performLineItemCreativeAssociationAction(
                                   {"xsi_type": "DeleteLineItemCreativeAssociations"},
                                   statement.ToStatement()), "numChanges")
            return {"numChanges": changes}

        step("delete creative association", delete_association)
        print(f"  note: creative {creative_id} is kept and reused by the next run")

    return finish()


def finish():
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
