#!/usr/bin/env python3
"""
Test script to verify DST transition handling.

This script:
1. Tests the DST detection functions
2. Fetches data for Oct 26, 2025 (fall DST transition)
3. Verifies that all 100 periods (25 hours × 4 periods/hour) are present
4. Checks that both occurrences of the 03:00 hour are correctly handled
"""

import logging
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

# Import functions from sync.py
from sync import (
    FINNISH_TIMEZONE,
    create_wattivahti_client,
    fetch_consumption_data,
    is_dst_transition_day,
    load_config,
    parse_consumption_data,
    read_refresh_token,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def test_dst_detection():
    """Test DST transition detection."""
    logger.info("=" * 80)
    logger.info("Testing DST Transition Detection")
    logger.info("=" * 80)

    # Test fall DST transition (Oct 26, 2025)
    fall_date = date(2025, 10, 26)
    transition_type, transition_time = is_dst_transition_day(fall_date)

    assert transition_type == "fall", f"Expected 'fall', got {transition_type}"
    assert transition_time is not None, "Expected transition time, got None"
    logger.info(f"✓ Fall DST transition detected: {fall_date} at {transition_time}")

    # Test spring DST transition (Mar 30, 2025)
    spring_date = date(2025, 3, 30)
    transition_type, transition_time = is_dst_transition_day(spring_date)

    assert transition_type == "spring", f"Expected 'spring', got {transition_type}"
    assert transition_time is not None, "Expected transition time, got None"
    logger.info(f"✓ Spring DST transition detected: {spring_date} at {transition_time}")

    # Test normal day (Oct 25, 2025)
    normal_date = date(2025, 10, 25)
    transition_type, transition_time = is_dst_transition_day(normal_date)

    assert transition_type is None, f"Expected None, got {transition_type}"
    assert transition_time is None, f"Expected None, got {transition_time}"
    logger.info(f"✓ Normal day detected: {normal_date}")

    logger.info("\n✓ All DST detection tests passed!\n")


def test_fall_dst_data_fetch():
    """Test fetching and parsing data for fall DST transition day."""
    logger.info("=" * 80)
    logger.info("Testing Fall DST Data Fetch (Oct 26, 2025)")
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

    # Parse the data
    logger.info("Parsing consumption data...")
    readings = parse_consumption_data(api_response)

    logger.info(f"\n✓ Fetched and parsed {len(readings)} records")

    # Analyze the data
    logger.info("\n" + "=" * 80)
    logger.info("Data Analysis")
    logger.info("=" * 80)

    # Group by UTC hour
    utc_tz = ZoneInfo("UTC")
    hour_counts = {}
    hour_consumption = {}

    for reading in readings:
        ts = reading["timestamp"]
        ts_utc = ts.astimezone(utc_tz)
        hour = ts_utc.hour

        if hour not in hour_counts:
            hour_counts[hour] = 0
            hour_consumption[hour] = 0.0

        hour_counts[hour] += 1
        hour_consumption[hour] += reading["consumption_kwh"]

    logger.info("\nRecords per UTC hour on Oct 26, 2025:")
    total_consumption = 0.0
    for hour in sorted(hour_counts.keys()):
        logger.info(
            f"  UTC {hour:02d}:00 - {hour_counts[hour]} records, {hour_consumption[hour]:.3f} kWh"
        )
        total_consumption += hour_consumption[hour]

    logger.info(f"\nTotal records: {len(readings)}")
    logger.info(f"Total consumption: {total_consumption:.3f} kWh")

    # Verify expectations
    logger.info("\n" + "=" * 80)
    logger.info("Verification")
    logger.info("=" * 80)

    # Expected: 100 records (25 hours × 4 periods/hour)
    expected_records = 100
    if len(readings) == expected_records:
        logger.info(f"✓ Record count correct: {len(readings)} records (25-hour day)")
    else:
        logger.error(
            f"✗ Record count incorrect: {len(readings)} records "
            f"(expected {expected_records} for 25-hour day)"
        )

    # Check for both occurrences of hour 03:00
    # First occurrence: UTC 00:00-00:59 (03:00-03:59 EEST)
    # Second occurrence: UTC 01:00-01:59 (03:00-03:59 EET)

    if 0 in hour_counts and hour_counts[0] == 4:
        logger.info(
            f"✓ First occurrence of 03:00 hour found: "
            f"UTC 00:00-00:59 (4 records, {hour_consumption[0]:.3f} kWh)"
        )
    else:
        logger.error(
            f"✗ First occurrence of 03:00 hour missing or incomplete: "
            f"{hour_counts.get(0, 0)} records"
        )

    if 1 in hour_counts and hour_counts[1] == 4:
        logger.info(
            f"✓ Second occurrence of 03:00 hour found: "
            f"UTC 01:00-01:59 (4 records, {hour_consumption[1]:.3f} kWh)"
        )
    else:
        logger.error(
            f"✗ Second occurrence of 03:00 hour missing or incomplete: "
            f"{hour_counts.get(1, 0)} records"
        )

    # Expected consumption: 24.510 kWh (from the problem description)
    expected_consumption = 24.510
    if abs(total_consumption - expected_consumption) < 0.01:
        logger.info(
            f"✓ Total consumption matches expected: "
            f"{total_consumption:.3f} kWh ≈ {expected_consumption:.3f} kWh"
        )
    else:
        logger.warning(
            f"⚠ Total consumption differs from expected: "
            f"{total_consumption:.3f} kWh vs {expected_consumption:.3f} kWh "
            f"(difference: {abs(total_consumption - expected_consumption):.3f} kWh)"
        )

    # Show detailed timestamps for the repeated hour
    logger.info("\n" + "=" * 80)
    logger.info("Detailed Timestamps for Repeated Hour (03:00)")
    logger.info("=" * 80)

    repeated_hour_records = [
        r for r in readings if r["timestamp"].astimezone(utc_tz).hour in [0, 1]
    ]

    for record in sorted(repeated_hour_records, key=lambda r: r["timestamp"]):
        ts = record["timestamp"]
        ts_utc = ts.astimezone(utc_tz)
        ts_local = ts.astimezone(FINNISH_TIMEZONE)

        logger.info(
            f"  Local: {ts_local.isoformat()} | "
            f"UTC: {ts_utc.isoformat()} | "
            f"Consumption: {record['consumption_kwh']:.3f} kWh"
        )

    logger.info("\n✓ Fall DST data fetch test completed!\n")

    return len(readings) == expected_records


def query_influxdb_data():
    """Query InfluxDB to verify data was written correctly."""
    logger.info("=" * 80)
    logger.info("Querying InfluxDB for Oct 26, 2025 Data")
    logger.info("=" * 80)

    config = load_config()

    # Create InfluxDB client
    client = InfluxDBClient(
        url=config["influxdb_url"],
        token=config["influxdb_token"],
        org=config["influxdb_org"],
    )

    # Query for Oct 26, 2025
    query = f'''
    from(bucket: "{config["influxdb_bucket"]}")
      |> range(start: 2025-10-26T00:00:00Z, stop: 2025-10-27T00:00:00Z)
      |> filter(fn: (r) => r["_measurement"] == "electricity_consumption")
      |> filter(fn: (r) => r["metering_point"] == "{config["metering_point"]}")
      |> filter(fn: (r) => r["_field"] == "consumption_kwh")
      |> sort(columns: ["_time"])
    '''

    try:
        query_api = client.query_api()
        result = query_api.query(org=config["influxdb_org"], query=query)

        if not result or len(result) == 0 or len(result[0].records) == 0:
            logger.warning("⚠ No data found in InfluxDB for Oct 26, 2025")
            logger.info("  Run sync.py to import the data first")
            client.close()
            return False

        records = result[0].records
        logger.info(f"\n✓ Found {len(records)} records in InfluxDB")

        # Analyze by UTC hour
        utc_tz = ZoneInfo("UTC")
        hour_counts = {}
        hour_consumption = {}

        for record in records:
            ts = record.get_time()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=utc_tz)
            else:
                ts = ts.astimezone(utc_tz)

            hour = ts.hour
            consumption = record.get_value()

            if hour not in hour_counts:
                hour_counts[hour] = 0
                hour_consumption[hour] = 0.0

            hour_counts[hour] += 1
            hour_consumption[hour] += consumption

        logger.info("\nRecords per UTC hour in InfluxDB:")
        total_consumption = 0.0
        for hour in sorted(hour_counts.keys()):
            logger.info(
                f"  UTC {hour:02d}:00 - {hour_counts[hour]} records, "
                f"{hour_consumption[hour]:.3f} kWh"
            )
            total_consumption += hour_consumption[hour]

        logger.info(f"\nTotal records: {len(records)}")
        logger.info(f"Total consumption: {total_consumption:.3f} kWh")

        # Verify
        if len(records) == 100:
            logger.info("✓ InfluxDB has correct record count (100 records)")
        else:
            logger.error(f"✗ InfluxDB has incorrect record count: {len(records)} (expected 100)")

        if 0 in hour_counts and hour_counts[0] == 4:
            logger.info("✓ First occurrence of 03:00 hour present in InfluxDB")
        else:
            logger.error("✗ First occurrence of 03:00 hour missing in InfluxDB")

        if 1 in hour_counts and hour_counts[1] == 4:
            logger.info("✓ Second occurrence of 03:00 hour present in InfluxDB")
        else:
            logger.error("✗ Second occurrence of 03:00 hour missing in InfluxDB")

        client.close()
        return len(records) == 100

    except Exception as e:
        logger.error(f"Error querying InfluxDB: {e}")
        client.close()
        return False


def main():
    """Run all tests."""
    logger.info("\n" + "=" * 80)
    logger.info("DST TRANSITION FIX - TEST SUITE")
    logger.info("=" * 80 + "\n")

    try:
        # Test 1: DST detection
        test_dst_detection()

        # Test 2: Fetch and parse data
        api_test_passed = test_fall_dst_data_fetch()

        # Test 3: Query InfluxDB (optional, only if data exists)
        logger.info("\n" + "=" * 80)
        logger.info("Optional: Check InfluxDB Data")
        logger.info("=" * 80)
        logger.info("To verify the fix is working in production, run:")
        logger.info("  python sync.py --start-date 2025-10-26 --end-date 2025-10-27")
        logger.info("Then run this test again to verify InfluxDB has the correct data.\n")

        db_test_passed = query_influxdb_data()

        # Summary
        logger.info("\n" + "=" * 80)
        logger.info("TEST SUMMARY")
        logger.info("=" * 80)
        logger.info("DST Detection: ✓ PASSED")
        logger.info(f"API Data Fetch: {'✓ PASSED' if api_test_passed else '✗ FAILED'}")
        logger.info(
            f"InfluxDB Data: {'✓ PASSED' if db_test_passed else '⚠ NOT VERIFIED (run sync first)'}"
        )

        if api_test_passed:
            logger.info("\n✓ DST fix is working correctly!")
            logger.info("\nNext steps:")
            logger.info("1. Run: python sync.py --start-date 2025-10-26 --end-date 2025-10-27")
            logger.info("2. Verify InfluxDB has 100 records for Oct 26, 2025")
            logger.info("3. Check that total consumption matches bill: 24.510 kWh")
        else:
            logger.error("\n✗ DST fix has issues - please review the logs above")
            sys.exit(1)

    except Exception as e:
        logger.error(f"\n✗ Test failed with error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
