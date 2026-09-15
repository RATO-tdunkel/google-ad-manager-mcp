"""Tests for MCP tool parameter handling at the server boundary.

MCP clients derive parameter types from the published JSON schema. Optional
parameters are published as an `anyOf` with a null branch, and clients that
collapse that away serialize a purely numeric identifier such as a network code
as a JSON number. These tests pin down that both serializations are accepted and
reach the underlying tool with the identical string value.
"""

import json
from unittest.mock import patch

import pytest
from fastmcp import Client

from gam_mcp.server import mcp

NETWORK_CODE = "98765432109"
AD_UNIT_ID = "123456789"


@pytest.fixture
def client():
    """In-memory MCP client with GAM credential bootstrapping stubbed out."""
    with patch("gam_mcp.server.init_client"):
        yield Client(mcp)


async def call(client, tool, arguments):
    """Call a tool over MCP and return the parsed JSON payload."""
    async with client:
        result = await client.call_tool(tool, arguments)
    return json.loads(result.content[0].text)


class TestNumericIdentifierParameters:
    """Numeric identifiers must work as JSON numbers and as JSON strings."""

    @pytest.mark.parametrize("network_code", [NETWORK_CODE, int(NETWORK_CODE)])
    async def test_network_code_accepts_number_and_string(self, client, network_code):
        """Test both serializations reach the tool as the same string."""
        with patch("gam_mcp.server.orders.list_delivering_orders", return_value={}) as tool:
            await call(client, "list_delivering_orders", {"network_code": network_code})

        tool.assert_called_once_with(network_code=NETWORK_CODE)

    @pytest.mark.parametrize("network_code", [NETWORK_CODE, int(NETWORK_CODE)])
    async def test_report_network_code_accepts_number_and_string(self, client, network_code):
        """Test the reporting tools normalize network_code the same way."""
        with patch("gam_mcp.server.reporting.run_custom_report", return_value={}) as tool:
            await call(client, "run_custom_report", {
                "dimensions": '["AD_UNIT_NAME"]',
                "columns": '["TOTAL_AD_REQUESTS"]',
                "network_code": network_code,
            })

        assert tool.call_args.kwargs["network_code"] == NETWORK_CODE

    @pytest.mark.parametrize("ad_unit_id", [AD_UNIT_ID, int(AD_UNIT_ID)])
    async def test_ad_unit_id_accepts_number_and_string(self, client, ad_unit_id):
        """Test ad_unit_id works as a filter regardless of serialization."""
        with patch("gam_mcp.server.reporting.run_inventory_report", return_value={}) as tool:
            await call(client, "run_inventory_report", {"ad_unit_id": ad_unit_id})

        assert tool.call_args.kwargs["ad_unit_id"] == AD_UNIT_ID

    @pytest.mark.parametrize("ad_unit_id", [AD_UNIT_ID, int(AD_UNIT_ID)])
    async def test_target_ad_unit_id_accepts_number_and_string(self, client, ad_unit_id):
        """Test the required ad unit target is normalized too."""
        with patch("gam_mcp.server.line_items.create_line_item", return_value={}) as tool:
            await call(client, "create_line_item", {
                "order_id": 1,
                "name": "Test Line Item",
                "end_year": 2026,
                "end_month": 12,
                "end_day": 31,
                "target_ad_unit_id": ad_unit_id,
            })

        assert tool.call_args.kwargs["target_ad_unit_id"] == AD_UNIT_ID

    async def test_padded_network_code_is_trimmed(self, client):
        """Test a stray space no longer turns a valid code into an unknown one."""
        with patch("gam_mcp.server.orders.list_delivering_orders", return_value={}) as tool:
            await call(client, "list_delivering_orders", {"network_code": f" {NETWORK_CODE}"})

        tool.assert_called_once_with(network_code=NETWORK_CODE)

    async def test_omitted_network_code_stays_none(self, client):
        """Test the default network is still selected when nothing is passed."""
        with patch("gam_mcp.server.orders.list_delivering_orders", return_value={}) as tool:
            await call(client, "list_delivering_orders", {})

        tool.assert_called_once_with(network_code=None)


class TestPublishedSchema:
    """The published schema must advertise both accepted types."""

    @pytest.mark.parametrize(
        "tool_name,param",
        [
            ("list_delivering_orders", "network_code"),
            ("run_custom_report", "network_code"),
            ("run_inventory_report", "ad_unit_id"),
            ("create_line_item", "target_ad_unit_id"),
        ],
    )
    async def test_identifier_params_allow_string_and_integer(self, tool_name, param):
        """Test a client reading the schema sees that a number is acceptable."""
        tools = await mcp.get_tools()
        schema = tools[tool_name].parameters["properties"][param]

        types = {branch["type"] for branch in schema["anyOf"]}
        assert {"string", "integer"} <= types


class TestAdUnitViewSchema:
    """ad_unit_view must be visible and documented on both report tools."""

    @pytest.mark.parametrize("tool_name", ["run_custom_report", "run_inventory_report"])
    async def test_ad_unit_view_is_exposed(self, tool_name):
        """Test the parameter is published with TOP_LEVEL as default."""
        tools = await mcp.get_tools()
        tool = tools[tool_name]

        assert tool.parameters["properties"]["ad_unit_view"]["default"] == "TOP_LEVEL"
        for view in ("TOP_LEVEL", "FLAT", "HIERARCHICAL"):
            assert view in tool.description

    @pytest.mark.parametrize("tool_name", ["run_custom_report", "run_inventory_report"])
    async def test_invalid_ad_unit_view_returns_error(self, client, tool_name):
        """Test an unknown view is reported clearly instead of hitting the API."""
        arguments = {"ad_unit_view": "AD_UNIT_NAME_ALL_LEVEL"}
        if tool_name == "run_custom_report":
            arguments |= {"dimensions": '["AD_UNIT_NAME"]', "columns": '["TOTAL_AD_REQUESTS"]'}

        payload = await call(client, tool_name, arguments)

        assert "Invalid ad_unit_view" in payload["error"]
