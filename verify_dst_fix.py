#!/usr/bin/env python3
"""
Detailed verification script to confirm DST fix matches Excel bill data.

Verifies that each individual period in the repeated hour matches the
expected consumption values from the electricity bill.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sync import (
    FINNISH_TIMEZONE,
    create_wattivahti_client,
    fetch_consumption_data,
    load_config,
    parse_consumption_data,
    read_refresh_token,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def verify_dst_fix():
    """Verify that the DST fix produces correct data matching the Excel bill."""
    logger.info("=" * 80)
    logger.info("DETAILED VERIFICATION: DST Fix vs Excel Bill Data")
    logger.info("=" * 80)

    # Load configuration
    config = load_config()
    refresh_token = read_refresh_token(config["refresh_token_file"])

    # Authenticate
    logger.info("\nAuthenticating...")
    client = create_wattivahti_client()
    token = client.refresh_token(refresh_token)

    # Fetch and parse data for Oct 26, 2025
    start_date = datetime(2025, 10, 26, 0, 0, 0, tzinfo=FINNISH_TIMEZONE)
    end_date = datetime(2025, 10, 27, 0, 0, 0, tzinfo=FINNISH_TIMEZONE)

    logger.info("Fetching Oct 26, 2025 data...")
    api_response = fetch_consumption_data(
        config["metering_point"],
        token.access_token,
        start_date,
        end_date,
        resolution="PT15MIN",
    )

    logger.info("Parsing data with DST handling...")
    readings = parse_consumption_data(api_response)

    logger.info(f"Total records parsed: {len(readings)}\n")

    # Expected values from Excel bill for the repeated hour (03:00)
    # First occurrence (EEST, UTC+3 → UTC 00:00-00:59)
    expected_first_03 = {
        0: 0.102,  # 03:00-03:14
        15: 0.098,  # 03:15-03:29
        30: 0.101,  # 03:30-03:44
        45: 0.108,  # 03:45-03:59
    }

    # Second occurrence (EET, UTC+2 → UTC 01:00-01:59)
    expected_second_03 = {
        0: 0.116,  # 03:00-03:14
        15: 0.096,  # 03:15-03:29
        30: 0.106,  # 03:30-03:44
        45: 0.093,  # 03:45-03:59
    }

    # Extract readings for the repeated hour
    utc_tz = ZoneInfo("UTC")
    first_occurrence = {}  # UTC 00:00-00:59
    second_occurrence = {}  # UTC 01:00-01:59

    for reading in readings:
        ts = reading["timestamp"]
        ts_utc = ts.astimezone(utc_tz)

        if ts_utc.date() == datetime(2025, 10, 26).date():
            if ts_utc.hour == 0:
                first_occurrence[ts_utc.minute] = reading["consumption_kwh"]
            elif ts_utc.hour == 1:
                second_occurrence[ts_utc.minute] = reading["consumption_kwh"]

    # Verify first occurrence
    logger.info("=" * 80)
    logger.info("FIRST OCCURRENCE (03:00 EEST → UTC 00:00-00:59)")
    logger.info("=" * 80)

    first_match = True
    first_total = 0.0
    for minute, expected in expected_first_03.items():
        actual = first_occurrence.get(minute, 0.0)
        first_total += actual
        match = abs(actual - expected) < 0.001
        status = "✓" if match else "✗"
        logger.info(
            f"{status} UTC 00:{minute:02d} - Expected: {expected:.3f} kWh, Actual: {actual:.3f} kWh"
        )
        if not match:
            first_match = False

    logger.info(f"\nFirst occurrence total: {first_total:.3f} kWh (expected ~0.409 kWh)")

    # Verify second occurrence
    logger.info("\n" + "=" * 80)
    logger.info("SECOND OCCURRENCE (03:00 EET → UTC 01:00-01:59)")
    logger.info("=" * 80)

    second_match = True
    second_total = 0.0
    for minute, expected in expected_second_03.items():
        actual = second_occurrence.get(minute, 0.0)
        second_total += actual
        match = abs(actual - expected) < 0.001
        status = "✓" if match else "✗"
        logger.info(
            f"{status} UTC 01:{minute:02d} - Expected: {expected:.3f} kWh, Actual: {actual:.3f} kWh"
        )
        if not match:
            second_match = False

    logger.info(f"\nSecond occurrence total: {second_total:.3f} kWh (expected ~0.411 kWh)")

    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("VERIFICATION SUMMARY")
    logger.info("=" * 80)

    if first_match and second_match:
        logger.info("✓ ALL VALUES MATCH - DST fix is working perfectly!")
        logger.info(f"✓ Total for both occurrences: {first_total + second_total:.3f} kWh")
        logger.info(f"✓ Total records: {len(readings)} (expected 100)")
        logger.info(
            f"✓ Total consumption: {sum(r['consumption_kwh'] for r in readings):.3f} kWh "
            f"(expected 24.510 kWh)"
        )
        logger.info("\nThe fix correctly handles both occurrences of the repeated hour!")
        logger.info("You can now re-import Oct 26, 2025 to fix the missing data.")
        return True
    else:
        logger.error("✗ VERIFICATION FAILED - Some values don't match")
        if not first_match:
            logger.error("  ✗ First occurrence (UTC 00:00) has incorrect values")
        if not second_match:
            logger.error("  ✗ Second occurrence (UTC 01:00) has incorrect values")
        return False


if __name__ == "__main__":
    import sys

    success = verify_dst_fix()
    sys.exit(0 if success else 1)
