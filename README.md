# AI SRE Agent

An AI-powered Site Reliability Engineer that monitors Docker containers running on AWS EC2 instances, investigates failures, and produces Root Cause Analysis (RCA) reports — automatically.

## What it does

You talk to it like a person. It does the investigation for you.

**Example conversations:**

> "I'm getting errors when I try to add items to my app on instance i-0abc123def456"

> "Check what's broken on i-0abc123def456 and give me an RCA"

> "Show me the service topology for my app"

The agent will:
- Check active alerts from Alertmanager
- List all Docker containers and their health
- Read container logs to find the exact error
- Check host memory, disk, and CPU
- Map how your services are connected (topology)
- Detect cascading failures
- Save a full RCA report as a markdown file

## What it can diagnose

| Fault | How it detects it |
|-------|-------------------|
| Missing environment variable | Reads container logs, finds `Error: X is not defined` |
| Wrong database hostname | Finds `MongooseServerSelectionError` in logs |
| Port mismatch (502 errors) | Reads nginx logs, finds upstream connection refused |
| OOM kill | Checks `OOMKilled` flag + kernel dmesg logs |
| Container crash loop | Exit code + restart count + log analysis |
| Disk full | Host diagnostics + container disk usage |
| Dependency down | Inspects dependency containers, checks connectivity |

## Tools available to the agent

- `list_containers` — see all containers and their state
- `inspect_container` — detailed info on a specific container
- `get_container_logs` — recent logs from a container
- `get_container_resource_usage` — live CPU and memory stats
- `get_container_disk_usage` — disk usage inside a container
- `get_docker_events` — timeline of container lifecycle events
- `get_host_diagnostics` — host disk, memory, CPU
- `get_host_oom_logs` — kernel OOM kill events from dmesg
- `get_active_alerts` — currently firing Alertmanager alerts
- `get_service_topology` — dependency map from docker-compose files
- `query_metrics` / `query_metrics_range` — Prometheus queries
- `list_metrics` / `get_metric_labels` — explore available metrics
- `save_rca_report` — saves structured RCA to a markdown file

## Output

Every investigation ends with a saved RCA report in `sre_agent/rca_reports/`.

Reports include: timeline, root cause (with exact log quotes), impact, immediate fix commands, and long-term recommendations.

## Architecture

```
You (chat) → ADK Agent (Gemini) → MCP Server (server.py) → AWS SSM → EC2 Instance
                                                          → Prometheus
                                                          → Alertmanager
```
