# WattiVahti to InfluxDB Sync

A CLI application that syncs electricity consumption data from WattiVahti API to InfluxDB. The script handles OAuth authentication, incremental data syncing, and token persistence. Can be run manually with specific date ranges or via cron for automatic incremental syncing.

## Features

- **Incremental Sync**: Automatically queries InfluxDB for the latest timestamp and syncs new data
- **Manual Date Range Sync**: Sync specific time periods with `--start-date` and `--end-date` arguments
- **Automatic Resolution Detection**: Tries PT15MIN first, falls back to PT1H for historical data
- **Token Management**: Automatically updates refresh tokens when rotated
- **Docker Compose Setup**: Local InfluxDB development environment

## Prerequisites

- Python 3.10 or higher
- `uv` (Astral toolchain) - see [AGENTS.md](AGENTS.md)
- Docker and Docker Compose (for local development)
- WattiVahti refresh token

## Local Development Setup

### 1. Install Dependencies

```bash
uv sync
```

### 2. Start InfluxDB

```bash
docker-compose up -d
```

### 3. Initialize InfluxDB

1. Access the InfluxDB UI at http://localhost:8086
2. Complete the initial setup (first time only)
3. Generate an API token with read/write permissions
4. Copy the token to your `.env` file as `INFLUXDB_TOKEN`

### 4. Configure Environment

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Required configuration:
- `INFLUXDB_TOKEN`: Token from InfluxDB setup
- `WATTIVAHTI_METERING_POINT`: Your 7-digit metering point code
- Other values have sensible defaults

### 5. Set Up Refresh Token

Create `refresh_token.txt` (or path specified by `REFRESH_TOKEN_FILE`) and write your WattiVahti refresh token to it.

### 6. Run Sync

**Incremental sync** (default, for cron):
```bash
uv run sync.py
```

**Manual date range sync**:
```bash
uv run sync.py --start-date 2024-01-01 --end-date 2024-01-31
uv run sync.py --start-date 2024-01-01T00:00:00 --end-date 2024-01-31T23:59:59
```

## Configuration

### Environment Variables

- `INFLUXDB_URL` - InfluxDB server URL (default: `http://localhost:8086`)
- `INFLUXDB_TOKEN` - InfluxDB authentication token (required)
- `INFLUXDB_ORG` - InfluxDB organization (default: `wattivahti`)
- `INFLUXDB_BUCKET` - InfluxDB bucket name (default: `electricity`)
- `WATTIVAHTI_METERING_POINT` - 7-digit metering point code (required)
- `REFRESH_TOKEN_FILE` - Path to refresh token file (default: `refresh_token.txt`)
- `INITIAL_SYNC_DAYS` - Days to fetch on first run (default: `7`)
- `SYNC_BUFFER_HOURS` - Hours before latest timestamp to include (default: `2`)

### InfluxDB Data Structure

- **Measurement**: `electricity_consumption`
- **Fields**:
  - `consumption_kwh` (float) - Electricity consumption in kWh
  - `consumption_wh` (float) - Electricity consumption in Wh
  - `resolution` (string) - Data resolution: `PT15MIN` or `PT1H`
- **Tags**:
  - `metering_point` (string) - Metering point code
- **Timestamp**: ISO format from API

## Production Deployment

### CI/CD Pipeline

The project includes a GitHub Actions workflow (`.github/workflows/deploy.yml`) that:

1. Builds and tests the application
2. Deploys to target server via self-hosted runner
3. Populates `.env` file from GitHub Environment secrets
4. Sets up cron job to run hourly

### Setting Up Cron Job

For manual setup, add to crontab:

```bash
0 * * * * cd /path/to/wattivahti-influx && uv run sync.py
```

This runs the sync every hour. The script automatically handles incremental syncing.

## How It Works

### Incremental Sync (Default)

1. Queries InfluxDB for the latest timestamp
2. Fetches data from (latest timestamp - buffer) to now
3. Uses buffer to prevent gaps due to clock skew or API timing
4. InfluxDB automatically deduplicates records with matching timestamps and values

### Resolution Detection

1. First attempts to fetch data with `PT15MIN` resolution (15 minutes)
2. If no data is returned, automatically retries with `PT1H` resolution (1 hour)
3. Stores the actual resolution used in each record
4. Handles the transition from PT1H to PT15MIN automatically without hardcoded dates

### Token Management

- Initial refresh token is written manually to the file
- If token is rotated during authentication, the new token is automatically saved
- Clear error messages if token file is missing or expired

## Error Handling

If the refresh token file is missing or expired, you'll see a clear error message indicating:
- The exact file path where the token should be written
- What needs to be written there
- Instructions on how to obtain the refresh token

## Toolchain

This project uses the Astral toolchain:
- `uv` - Package management
- `ruff` - Linting and formatting
- `ty` - Type checking

See [AGENTS.md](AGENTS.md) for details.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

