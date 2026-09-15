"""Tests for reporting tools."""

from unittest.mock import MagicMock, patch

import pytest

from gam_mcp.tools import reporting

CSV_REPORT = (
    "Dimension.AD_UNIT_NAME,Column.TOTAL_AD_REQUESTS\n"
    "Site_Desktop_News_details_ad_1,1234\n"
)


@pytest.fixture
def mock_report_client():
    """Mock the GAM client used by the reporting module.

    Yields the ReportService mock so tests can inspect the submitted report job.
    """
    client = MagicMock()
    report_service = MagicMock()
    report_service.runReportJob.return_value = {"id": 42}
    report_service.getReportJobStatus.return_value = "COMPLETED"
    client.get_service.return_value = report_service

    def download(job_id, export_format, buffer):
        buffer.write(CSV_REPORT.encode("utf-8"))

    downloader = MagicMock()
    downloader.DownloadReportToFile.side_effect = download
    client.get_data_downloader.return_value = downloader

    with patch.object(reporting, "get_gam_client", return_value=client):
        yield report_service


def submitted_query(report_service):
    """Return the reportQuery that was handed to runReportJob."""
    report_service.runReportJob.assert_called_once()
    return report_service.runReportJob.call_args[0][0]["reportQuery"]


class TestAdUnitView:
    """Tests for the ad_unit_view parameter."""

    def test_defaults_to_top_level(self, mock_report_client):
        """Omitting ad_unit_view keeps GAM's existing TOP_LEVEL behaviour."""
        result = reporting.run_custom_report(
            dimensions=["AD_UNIT_NAME"], columns=["TOTAL_AD_REQUESTS"]
        )

        assert result["success"] is True
        assert submitted_query(mock_report_client)["adUnitView"] == "TOP_LEVEL"

    @pytest.mark.parametrize("view", ["TOP_LEVEL", "FLAT", "HIERARCHICAL"])
    def test_valid_views_land_in_query(self, mock_report_client, view):
        """Every allowed view is passed through as reportQuery.adUnitView."""
        result = reporting.run_custom_report(
            dimensions=["AD_UNIT_NAME"],
            columns=["TOTAL_AD_REQUESTS"],
            ad_unit_view=view,
        )

        assert submitted_query(mock_report_client)["adUnitView"] == view
        assert result["ad_unit_view"] == view

    def test_inventory_report_forwards_view(self, mock_report_client):
        """run_inventory_report hands ad_unit_view down to run_custom_report."""
        reporting.run_inventory_report(ad_unit_view="FLAT")

        assert submitted_query(mock_report_client)["adUnitView"] == "FLAT"

    def test_inventory_report_defaults_to_top_level(self, mock_report_client):
        """run_inventory_report keeps TOP_LEVEL as its default."""
        reporting.run_inventory_report()

        assert submitted_query(mock_report_client)["adUnitView"] == "TOP_LEVEL"

    @pytest.mark.parametrize("view", ["AD_UNIT_NAME_ALL_LEVEL", "flat", "", "LEAF"])
    def test_invalid_view_is_rejected(self, mock_report_client, view):
        """Invalid values are rejected locally instead of reaching the API."""
        result = reporting.run_custom_report(
            dimensions=["AD_UNIT_NAME"],
            columns=["TOTAL_AD_REQUESTS"],
            ad_unit_view=view,
        )

        assert "error" in result
        assert "ad_unit_view" in result["error"]
        assert "TOP_LEVEL, FLAT, HIERARCHICAL" in result["error"]
        mock_report_client.runReportJob.assert_not_called()

    def test_invalid_view_in_inventory_report_is_rejected(self, mock_report_client):
        """The same validation applies through run_inventory_report."""
        result = reporting.run_inventory_report(ad_unit_view="LEAF")

        assert "error" in result
        mock_report_client.runReportJob.assert_not_called()
