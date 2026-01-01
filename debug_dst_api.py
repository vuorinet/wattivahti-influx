#!/usr/bin/env python3
"""
Debug script to examine the raw API response for DST transition day.

This script fetches the raw data for Oct 26, 2025 and prints detailed
information about how the API structures the repeated hour data.
"""

import json
import logging
import sys
from datetime import datetime

from sync import (
    FINNISH_TIMEZONE,
    create_wattivahti_client,
    fetch_consumption_data,
    load_config,
    read_refresh_token,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def debug_api_response():
    """Fetch and analyze the raw API response for Oct 26, 2025."""
    logger.info("=" * 80)
    logger.info("DEBUG: Raw API Response for Oct 26, 2025")
    logger.info("=" * 80)

    # Load configuration
    config = load_config()

    # Read refresh token
    refresh_token = read_refresh_token(config["refresh_token_file"])

    # Authenticate
    logger.info("Authenticating with WattiVahti...")
    client = create_wattivahti_client()
    token = client.refresh_token(refresh_token)
    logger.info("✓ Authentication successful")

    # Fetch data for Oct 26, 2025 (fall DST transition)
    start_date = datetime(2025, 10, 26, 0, 0, 0, tzinfo=FINNISH_TIMEZONE)
    end_date = datetime(2025, 10, 27, 0, 0, 0, tzinfo=FINNISH_TIMEZONE)

    logger.info(f"Fetching data from {start_date} to {end_date}")
    api_response = fetch_consumption_data(
        config["metering_point"],
        token.access_token,
        start_date,
        end_date,
        resolution="PT15MIN",
    )

    # Save raw response to file
    with open("dst_api_response.json", "w") as f:
        json.dump(api_response, f, indent=2)
    logger.info("✓ Saved raw API response to dst_api_response.json")

    # Extract TSV data
    try:
        result = api_response.get("getconsumptionsresult", {})
        consumption_data = result.get("consumptiondata", {})
        timeseries = consumption_data.get("timeseries", {})
        values = timeseries.get("values", {})
        tsv_data = values.get("tsv", [])

        logger.info(f"\n✓ Found {len(tsv_data)} records in API response")

        # Group by hour to find the repeated hour
        from collections import defaultdict

        data_by_hour = defaultdict(list)
        for item in tsv_data:
            time_str = item.get("time", "")
            if time_str.endswith("Z"):
                time_str = time_str[:-1]

            dt = datetime.fromisoformat(time_str)
            hour = dt.hour
            data_by_hour[hour].append(item)

        # Focus on hour 03:00 (the repeated hour)
        logger.info("\n" + "=" * 80)
        logger.info("HOUR 03:00 (REPEATED HOUR) - Raw Data")
        logger.info("=" * 80)

        hour_03_data = data_by_hour.get(3, [])
        logger.info(f"Found {len(hour_03_data)} records for hour 03:00")

        if len(hour_03_data) == 8:
            logger.info("\n✓ Expected count: 8 records (4 for each occurrence)")
        else:
            logger.warning(f"\n⚠ Unexpected count: {len(hour_03_data)} records (expected 8)")

        logger.info("\nAll records for hour 03:00 (in API response order):")
        for idx, item in enumerate(hour_03_data, 1):
            time_str = item.get("time", "")
            consumption = item.get("quantity", 0)
            logger.info(f"  {idx}. Time: {time_str}, Consumption: {consumption} kWh")

        # Group by minute to see duplicates
        logger.info("\n" + "=" * 80)
        logger.info("GROUPED BY MINUTE (Detecting Duplicates)")
        logger.info("=" * 80)

        from collections import defaultdict

        by_minute = defaultdict(list)
        for item in hour_03_data:
            time_str = item.get("time", "")
            if time_str.endswith("Z"):
                time_str = time_str[:-1]
            dt = datetime.fromisoformat(time_str)
            minute_key = f"{dt.hour:02d}:{dt.minute:02d}"
            by_minute[minute_key].append({"time": time_str, "consumption": item.get("quantity", 0)})

        for minute_key in sorted(by_minute.keys()):
            records = by_minute[minute_key]
            logger.info(f"\n{minute_key} - {len(records)} record(s):")
            for idx, rec in enumerate(records, 1):
                logger.info(f"  Occurrence {idx}: {rec['time']} = {rec['consumption']} kWh")

        # Check the timestamps more carefully
        logger.info("\n" + "=" * 80)
        logger.info("TIMESTAMP ANALYSIS")
        logger.info("=" * 80)

        # Check if timestamps have timezone info or other markers
        sample = hour_03_data[0] if hour_03_data else {}
        logger.info("\nSample record structure:")
        logger.info(json.dumps(sample, indent=2))

        # Analyze all unique fields in the records
        all_keys = set()
        for item in hour_03_data:
            all_keys.update(item.keys())
        logger.info(f"\nAll fields in records: {sorted(all_keys)}")

    except Exception as e:
        logger.error(f"Error parsing API response: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    debug_api_response()
