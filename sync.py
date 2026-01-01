#!/usr/bin/env python3
"""
WattiVahti to InfluxDB Sync

Syncs electricity consumption data from WattiVahti API to InfluxDB.
Supports both incremental sync (for cron jobs) and manual date range sync.
"""

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from b2c_oauth_client import AuthenticationError, B2COAuthClient
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# WattiVahti Configuration Constants
WATTIVAHTI_TENANT = "pesv.onmicrosoft.com"
WATTIVAHTI_CLIENT_ID = "84ebdb93-9ea6-42c7-bd7d-302abf7556fa"
WATTIVAHTI_POLICY = "B2C_1_Tunnistus_SignInv2"
WATTIVAHTI_SCOPE = "https://pesv.onmicrosoft.com/salpa/customer.read openid profile offline_access"
WATTIVAHTI_API_BASE = "https://porienergia-prod-agent.frendsapp.com:9999/api/onlineapi/v1"
FINNISH_TIMEZONE = ZoneInfo("Europe/Helsinki")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Cache for DST transition detection to avoid repeated calculations
_dst_transition_cache: dict[tuple[date, str], tuple[str | None, datetime | None]] = {}


def is_dst_transition_day(
    target_date: date, tz_name: str = "Europe/Helsinki"
) -> tuple[str | None, datetime | None]:
    """
    Detect if a date is a DST transition day in the specified timezone.

    In Finland (Europe/Helsinki):
    - Spring: Last Sunday of March at 03:00 EET → 04:00 EEST (23-hour day)
    - Fall: Last Sunday of October at 04:00 EEST → 03:00 EET (25-hour day)

    Args:
        target_date: The date to check
        tz_name: The timezone name (default: Europe/Helsinki)

    Returns:
        Tuple of (transition_type, transition_datetime):
        - transition_type: 'spring', 'fall', or None
        - transition_datetime: The local time when transition occurs, or None
    """
    # Check cache first
    cache_key = (target_date, tz_name)
    if cache_key in _dst_transition_cache:
        return _dst_transition_cache[cache_key]

    tz = ZoneInfo(tz_name)
    year = target_date.year
    result = (None, None)

    # Find last Sunday of March (spring transition)
    for day in range(31, 24, -1):
        try:
            dt = datetime(year, 3, day, tzinfo=tz)
            if dt.weekday() == 6:  # Sunday
                if dt.date() == target_date:
                    # Spring transition: clocks go forward at 03:00 EET
                    transition_time = datetime(year, 3, day, 3, 0, 0, tzinfo=tz)
                    logger.info(f"Detected spring DST transition day: {target_date}")
                    result = ("spring", transition_time)
                break
        except ValueError:
            continue

    # Find last Sunday of October (fall transition)
    if result == (None, None):
        for day in range(31, 24, -1):
            try:
                dt = datetime(year, 10, day, tzinfo=tz)
                if dt.weekday() == 6:  # Sunday
                    if dt.date() == target_date:
                        # Fall transition: clocks go back at 04:00 EEST → 03:00 EET
                        # The repeated hour is 03:00-03:59
                        transition_time = datetime(year, 10, day, 3, 0, 0, tzinfo=tz, fold=0)
                        logger.info(f"Detected fall DST transition day: {target_date}")
                        result = ("fall", transition_time)
                    break
            except ValueError:
                continue

    # Cache the result
    _dst_transition_cache[cache_key] = result
    return result


def parse_timestamp_with_dst_handling(
    timestamp_str: str,
    target_date: date,
    occurrence: int = 0,
    tz: ZoneInfo = FINNISH_TIMEZONE,
) -> datetime:
    """
    Parse timestamp string to datetime, handling DST transitions correctly.

    During fall DST transition, the hour 03:00-03:59 occurs twice. This
    function uses the 'fold' parameter to disambiguate:
    - fold=0: First occurrence (before transition, EEST, UTC+3)
    - fold=1: Second occurrence (after transition, EET, UTC+2)

    Args:
        timestamp_str: ISO format timestamp string
        target_date: The date this timestamp belongs to (for DST detection)
        occurrence: Which occurrence of an ambiguous time (0=first, 1=second)
        tz: The timezone to use if none is specified

    Returns:
        Timezone-aware datetime object
    """
    # Remove 'Z' suffix if present
    if timestamp_str.endswith("Z"):
        timestamp_str = timestamp_str[:-1]

    # Parse the timestamp
    dt = datetime.fromisoformat(timestamp_str)

    # If already has timezone info, convert to target timezone
    if dt.tzinfo is not None:
        return dt.astimezone(tz)

    # Check if this is a DST transition day
    transition_type, transition_time = is_dst_transition_day(target_date, tz.key)

    if transition_type == "fall":
        # During fall transition, hour 03:00-03:59 is ambiguous
        # Use fold parameter to disambiguate
        hour = dt.hour
        if hour == 3:
            # This is the repeated hour - use fold to disambiguate
            dt = dt.replace(tzinfo=tz, fold=occurrence)
            logger.debug(
                f"Parsed ambiguous time {timestamp_str} as fold={occurrence} "
                f"(UTC: {dt.astimezone(ZoneInfo('UTC')).isoformat()})"
            )
        else:
            dt = dt.replace(tzinfo=tz)
    elif transition_type == "spring":
        # During spring transition, hour 03:00-03:59 doesn't exist
        # If we encounter it, log a warning
        hour = dt.hour
        if hour == 3:
            logger.warning(
                f"Timestamp {timestamp_str} falls in non-existent hour "
                f"during spring DST transition on {target_date}"
            )
        dt = dt.replace(tzinfo=tz)
    else:
        # Normal day - no special handling
        dt = dt.replace(tzinfo=tz)

    return dt


def load_config() -> dict:
    """Load configuration from environment variables."""
    load_dotenv()

    config = {
        "influxdb_url": os.getenv("INFLUXDB_URL", "http://localhost:8086"),
        "influxdb_token": os.getenv("INFLUXDB_TOKEN"),
        "influxdb_org": os.getenv("INFLUXDB_ORG", "wattivahti"),
        "influxdb_bucket": os.getenv("INFLUXDB_BUCKET", "electricity"),
        "metering_point": os.getenv("WATTIVAHTI_METERING_POINT"),
        "refresh_token_file": os.getenv("REFRESH_TOKEN_FILE", "refresh_token.txt"),
        "initial_sync_days": int(os.getenv("INITIAL_SYNC_DAYS", "7")),
        "sync_buffer_hours": int(os.getenv("SYNC_BUFFER_HOURS", "2")),
    }

    # Validate required configuration
    if not config["influxdb_token"]:
        logger.error("INFLUXDB_TOKEN is required but not set")
        sys.exit(1)

    if not config["metering_point"]:
        logger.error("WATTIVAHTI_METERING_POINT is required but not set")
        sys.exit(1)

    return config


def read_refresh_token(token_file: str) -> str:
    """Read refresh token from file."""
    token_path = Path(token_file)

    if not token_path.exists():
        error_msg = (
            f"Refresh token file not found: {token_path.absolute()}\n"
            f"Please create this file and write your WattiVahti refresh token to it.\n"
            f"The refresh token is obtained from the WattiVahti authentication flow."
        )
        logger.error(error_msg)
        sys.exit(1)

    token = token_path.read_text().strip()

    if not token:
        error_msg = (
            f"Refresh token file is empty: {token_path.absolute()}\n"
            f"Please write your WattiVahti refresh token to this file."
        )
        logger.error(error_msg)
        sys.exit(1)

    return token


def save_refresh_token(token_file: str, token: str) -> None:
    """Save refresh token to file."""
    token_path = Path(token_file)
    token_path.write_text(token)
    logger.info(f"Saved refreshed token to {token_path.absolute()}")


def create_wattivahti_client() -> B2COAuthClient:
    """Create Azure B2C client configured for WattiVahti."""
    return B2COAuthClient(
        tenant=WATTIVAHTI_TENANT,
        client_id=WATTIVAHTI_CLIENT_ID,
        policy=WATTIVAHTI_POLICY,
        scope=WATTIVAHTI_SCOPE,
    )


def get_latest_timestamp_from_influxdb(
    client: InfluxDBClient, bucket: str, org: str, metering_point: str
) -> datetime | None:
    """Query InfluxDB for the latest timestamp."""
    query = f'''
    from(bucket: "{bucket}")
      |> range(start: -30d)
      |> filter(fn: (r) => r["_measurement"] == "electricity_consumption")
      |> filter(fn: (r) => r["metering_point"] == "{metering_point}")
      |> last()
      |> keep(columns: ["_time"])
    '''

    try:
        query_api = client.query_api()
        result = query_api.query(org=org, query=query)

        if result and len(result) > 0 and len(result[0].records) > 0:
            latest_time = result[0].records[0].get_time()
            # Convert to timezone-aware datetime
            if latest_time.tzinfo is None:
                latest_time = latest_time.replace(tzinfo=FINNISH_TIMEZONE)
            else:
                latest_time = latest_time.astimezone(FINNISH_TIMEZONE)
            logger.info(f"Found latest timestamp in InfluxDB: {latest_time}")
            return latest_time
        else:
            logger.info("No existing data found in InfluxDB")
            return None
    except Exception as e:
        logger.warning(f"Error querying InfluxDB for latest timestamp: {e}")
        return None


def parse_date_string(date_str: str) -> datetime:
    """Parse date string to Finnish timezone datetime."""
    if len(date_str) == 10:  # YYYY-MM-DD
        dt = datetime.fromisoformat(date_str + "T00:00:00")
    else:
        dt = datetime.fromisoformat(date_str)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FINNISH_TIMEZONE)
    else:
        dt = dt.astimezone(FINNISH_TIMEZONE)

    return dt


def fetch_consumption_data(
    metering_point: str,
    access_token: str,
    start_date: datetime,
    end_date: datetime,
    resolution: str = "PT15MIN",
) -> dict:
    """Fetch consumption data from WattiVahti API."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "wattivahti-influx-sync/1.0.0",
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
    )

    url = f"{WATTIVAHTI_API_BASE}/meterdata2"
    params = {
        "meteringPointCode": metering_point,
        "measurementType": "1",  # Consumption data
        "start": start_date.isoformat(),
        "stop": end_date.isoformat(),
        "resultStep": resolution,
    }

    try:
        logger.info(
            f"Fetching data from {start_date.isoformat()} to {end_date.isoformat()} "
            f"with resolution {resolution}"
        )
        response = session.get(url, params=params)

        if response.status_code != 200:
            raise Exception(f"API request failed: {response.status_code} - {response.text[:200]}")

        return response.json()

    except requests.RequestException as e:
        raise Exception(f"Network error: {e}")


def parse_consumption_data(api_response: dict) -> list[dict]:
    """
    Parse consumption data from API response, handling DST transitions.

    During fall DST transitions, the API may return data for the repeated
    hour twice. This function correctly handles the ambiguous timestamps by:
    1. Grouping data by date
    2. Detecting DST transition days
    3. Tracking which occurrence of the repeated hour we're processing
    4. Using the 'fold' parameter to disambiguate timestamps
    """
    try:
        result = api_response.get("getconsumptionsresult", {})
        consumption_data = result.get("consumptiondata", {})
        timeseries = consumption_data.get("timeseries", {})
        values = timeseries.get("values", {})
        tsv_data = values.get("tsv", [])

        if not tsv_data:
            return []

        # First pass: group raw data by date and local hour to detect duplicates
        data_by_date = defaultdict(lambda: defaultdict(list))

        for item in tsv_data:
            timestamp_str = item.get("time", "")
            consumption = item.get("quantity")

            if timestamp_str and consumption is not None:
                # Parse as naive datetime first to get the date
                if timestamp_str.endswith("Z"):
                    ts_clean = timestamp_str[:-1]
                else:
                    ts_clean = timestamp_str

                dt_naive = datetime.fromisoformat(ts_clean)
                dt_date = dt_naive.date()
                hour = dt_naive.hour

                data_by_date[dt_date][hour].append(
                    {
                        "timestamp_str": timestamp_str,
                        "consumption": float(consumption),
                        "unit": item.get("unit", "kWh"),
                        "naive_dt": dt_naive,
                    }
                )

        # Second pass: process data with DST awareness
        readings = []

        for target_date in sorted(data_by_date.keys()):
            transition_type, _ = is_dst_transition_day(target_date)

            if transition_type == "fall":
                # Fall DST transition: hour 03:00 occurs twice
                logger.info(
                    f"Processing fall DST transition day {target_date} (hour 03:00 repeats)"
                )

                # Track hour 03:00 occurrences separately
                hour_03_count = len(data_by_date[target_date].get(3, []))

                logger.info(
                    f"Found {hour_03_count} records for hour 03:00 on {target_date}"
                )

                for hour in sorted(data_by_date[target_date].keys()):
                    items = data_by_date[target_date][hour]

                    if hour == 3:
                        # Process repeated hour with correct fold values
                        # The API should return these in chronological order:
                        # - First 4 records: fold=0 (EEST, UTC+3, before transition)
                        # - Next 4 records: fold=1 (EET, UTC+2, after transition)

                        if hour_03_count == 8:
                            # Expected case: 8 records (4 for each occurrence)
                            logger.info("Processing 8 records for repeated hour 03:00 (4+4)")

                            for idx, item in enumerate(items):
                                # First 4 records: fold=0, next 4 records: fold=1
                                fold = 0 if idx < 4 else 1
                                timestamp = parse_timestamp_with_dst_handling(
                                    item["timestamp_str"],
                                    target_date,
                                    occurrence=fold,
                                    tz=FINNISH_TIMEZONE,
                                )

                                readings.append(
                                    {
                                        "timestamp": timestamp,
                                        "consumption_kwh": item["consumption"],
                                        "consumption_wh": item["consumption"] * 1000,
                                        "unit": item["unit"],
                                    }
                                )

                                logger.debug(
                                    f"  Record {idx + 1}/8: fold={fold}, "
                                    f"UTC={timestamp.astimezone(ZoneInfo('UTC')).isoformat()}, "
                                    f"consumption={item['consumption']} kWh"
                                )

                        elif hour_03_count == 4:
                            # Unexpected case: only 4 records (might be missing one occurrence)
                            logger.warning(
                                f"Only 4 records found for repeated hour 03:00 on {target_date}. "
                                f"Expected 8 records (4 for each occurrence). "
                                f"Data may be incomplete!"
                            )

                            # Process with fold=0 (first occurrence)
                            for item in items:
                                timestamp = parse_timestamp_with_dst_handling(
                                    item["timestamp_str"],
                                    target_date,
                                    occurrence=0,
                                    tz=FINNISH_TIMEZONE,
                                )

                                readings.append(
                                    {
                                        "timestamp": timestamp,
                                        "consumption_kwh": item["consumption"],
                                        "consumption_wh": item["consumption"] * 1000,
                                        "unit": item["unit"],
                                    }
                                )

                        else:
                            # Unexpected count
                            logger.warning(
                                f"Unexpected record count for hour 03:00 on {target_date}: "
                                f"{hour_03_count} records (expected 8 or 4)"
                            )

                            # Process with fold=0 by default
                            for item in items:
                                timestamp = parse_timestamp_with_dst_handling(
                                    item["timestamp_str"],
                                    target_date,
                                    occurrence=0,
                                    tz=FINNISH_TIMEZONE,
                                )

                                readings.append(
                                    {
                                        "timestamp": timestamp,
                                        "consumption_kwh": item["consumption"],
                                        "consumption_wh": item["consumption"] * 1000,
                                        "unit": item["unit"],
                                    }
                                )

                    else:
                        # Normal hour - no ambiguity
                        for item in items:
                            timestamp = parse_timestamp_with_dst_handling(
                                item["timestamp_str"],
                                target_date,
                                occurrence=0,
                                tz=FINNISH_TIMEZONE,
                            )

                            readings.append(
                                {
                                    "timestamp": timestamp,
                                    "consumption_kwh": item["consumption"],
                                    "consumption_wh": item["consumption"] * 1000,
                                    "unit": item["unit"],
                                }
                            )

            elif transition_type == "spring":
                # Spring DST transition: hour 03:00 doesn't exist (clocks jump forward)
                logger.info(
                    f"Processing spring DST transition day {target_date} "
                    f"(hour 03:00 doesn't exist, 23-hour day)"
                )

                # Check if we have data for the missing hour
                if 3 in data_by_date[target_date]:
                    logger.warning(
                        f"Found {len(data_by_date[target_date][3])} records for "
                        f"non-existent hour 03:00 on spring DST transition {target_date}"
                    )

                # Process all hours normally
                for hour in sorted(data_by_date[target_date].keys()):
                    for item in data_by_date[target_date][hour]:
                        timestamp = parse_timestamp_with_dst_handling(
                            item["timestamp_str"], target_date, occurrence=0, tz=FINNISH_TIMEZONE
                        )

                        readings.append(
                            {
                                "timestamp": timestamp,
                                "consumption_kwh": item["consumption"],
                                "consumption_wh": item["consumption"] * 1000,
                                "unit": item["unit"],
                            }
                        )

            else:
                # Normal day - no DST transition
                for hour in sorted(data_by_date[target_date].keys()):
                    for item in data_by_date[target_date][hour]:
                        timestamp = parse_timestamp_with_dst_handling(
                            item["timestamp_str"], target_date, occurrence=0, tz=FINNISH_TIMEZONE
                        )

                        readings.append(
                            {
                                "timestamp": timestamp,
                                "consumption_kwh": item["consumption"],
                                "consumption_wh": item["consumption"] * 1000,
                                "unit": item["unit"],
                            }
                        )

        logger.info(f"Parsed {len(readings)} total records from API response")
        return readings

    except (KeyError, ValueError, TypeError) as e:
        raise Exception(f"Failed to parse consumption data: {e}")


def fetch_data_with_resolution_fallback(
    metering_point: str,
    access_token: str,
    start_date: datetime,
    end_date: datetime,
) -> tuple[list[dict], str]:
    """
    Fetch data with automatic resolution detection.
    Tries PT15MIN first, falls back to PT1H if no data returned.
    Returns (readings, resolution_used)
    """
    # First attempt with PT15MIN
    logger.info("Attempting to fetch data with resolution PT15MIN")
    response = fetch_consumption_data(
        metering_point, access_token, start_date, end_date, resolution="PT15MIN"
    )

    readings = parse_consumption_data(response)

    if readings:
        logger.info(f"Successfully fetched {len(readings)} records with PT15MIN resolution")
        return readings, "PT15MIN"

    # Fallback to PT1H if no data
    logger.info("No data with PT15MIN, trying PT1H resolution")
    response = fetch_consumption_data(
        metering_point, access_token, start_date, end_date, resolution="PT1H"
    )

    readings = parse_consumption_data(response)

    if readings:
        logger.info(f"Successfully fetched {len(readings)} records with PT1H resolution")
        return readings, "PT1H"

    logger.warning("No data returned with either PT15MIN or PT1H resolution")
    return [], "PT15MIN"  # Default to PT15MIN even if no data


def write_to_influxdb(
    client: InfluxDBClient,
    bucket: str,
    org: str,
    metering_point: str,
    readings: list[dict],
    resolution: str,
) -> None:
    """Write consumption data to InfluxDB."""
    if not readings:
        logger.info("No data to write to InfluxDB")
        return

    write_api = client.write_api(write_options=SYNCHRONOUS)

    points = []
    for reading in readings:
        point = (
            Point("electricity_consumption")
            .tag("metering_point", metering_point)
            .field("consumption_kwh", reading["consumption_kwh"])
            .field("consumption_wh", reading["consumption_wh"])
            .field("resolution", resolution)
            .time(reading["timestamp"])
        )
        points.append(point)

    try:
        write_api.write(bucket=bucket, org=org, record=points)
        logger.info(f"Successfully wrote {len(points)} records to InfluxDB")
    except Exception as e:
        logger.error(f"Error writing to InfluxDB: {e}")
        raise


def main() -> None:
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Sync electricity consumption data from WattiVahti to InfluxDB"
    )
    parser.add_argument(
        "--start-date",
        help=(
            "Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). "
            "If not provided, uses latest timestamp from InfluxDB."
        ),
    )
    parser.add_argument(
        "--end-date",
        help="End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). Defaults to now.",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config()

    # Read refresh token
    refresh_token = read_refresh_token(config["refresh_token_file"])

    # Create WattiVahti client and authenticate
    logger.info("Authenticating with WattiVahti...")
    try:
        client = create_wattivahti_client()
        token = client.refresh_token(refresh_token)
        logger.info("Authentication successful")

        # Save refreshed token if it was rotated
        if token.refresh_token and token.refresh_token != refresh_token:
            save_refresh_token(config["refresh_token_file"], token.refresh_token)
    except AuthenticationError as e:
        error_msg = (
            f"Authentication failed: {e}\n"
            f"Please check your refresh token in: "
            f"{Path(config['refresh_token_file']).absolute()}\n"
            f"If the token is expired, you need to obtain a new refresh token "
            f"and write it to the file."
        )
        logger.error(error_msg)
        sys.exit(1)

    # Create InfluxDB client (used for both querying and writing)
    influx_client = InfluxDBClient(
        url=config["influxdb_url"],
        token=config["influxdb_token"],
        org=config["influxdb_org"],
    )

    # Determine date range
    if args.start_date:
        # Manual mode: use provided dates
        logger.info("Using manual date range")
        start_dt = parse_date_string(args.start_date)
        end_dt = (
            parse_date_string(args.end_date) if args.end_date else datetime.now(FINNISH_TIMEZONE)
        )
    else:
        # Incremental mode: query InfluxDB for latest timestamp
        logger.info("Using incremental sync mode")

        latest_timestamp = get_latest_timestamp_from_influxdb(
            influx_client,
            config["influxdb_bucket"],
            config["influxdb_org"],
            config["metering_point"],
        )

        if latest_timestamp:
            # Use latest timestamp minus buffer
            start_dt = latest_timestamp - timedelta(hours=config["sync_buffer_hours"])
            logger.info(
                f"Starting sync from {start_dt.isoformat()} "
                f"(latest timestamp - {config['sync_buffer_hours']}h buffer)"
            )
        else:
            # No data exists, use initial sync days
            start_dt = datetime.now(FINNISH_TIMEZONE) - timedelta(days=config["initial_sync_days"])
            logger.info(
                f"No existing data, fetching last {config['initial_sync_days']} days "
                f"from {start_dt.isoformat()}"
            )

        end_dt = datetime.now(FINNISH_TIMEZONE)

    # Fetch data with resolution fallback
    readings, resolution = fetch_data_with_resolution_fallback(
        config["metering_point"],
        token.access_token,
        start_dt,
        end_dt,
    )

    if not readings:
        logger.warning("No data to sync")
        influx_client.close()
        return

    write_to_influxdb(
        influx_client,
        config["influxdb_bucket"],
        config["influxdb_org"],
        config["metering_point"],
        readings,
        resolution,
    )

    influx_client.close()
    logger.info("Sync completed successfully")


if __name__ == "__main__":
    main()
