import json
import os
import time
from typing import Annotated

import boto3
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

mcp = FastMCP("docker-ssm-agent")
ssm = boto3.client("ssm")
PROM_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")


def _ssm(instance_id: str, command: str) -> str:
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]},
    )
    cmd_id = resp["Command"]["CommandId"]
    for _ in range(15):
        time.sleep(2)
        result = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        if result["Status"] in ("Success", "Failed", "TimedOut", "Cancelled"):
            stdout = result.get("StandardOutputContent", "")
            stderr = result.get("StandardErrorContent", "")
            if result["Status"] != "Success":
                return json.dumps({"error": stderr or result["Status"]})
            return stdout
    return json.dumps({"error": "Command timed out waiting for result."})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_containers(
    instance_id: Annotated[str, Field(description="EC2 instance ID to run the command on (e.g. 'i-0abc123def456').")],
) -> str:
    """List all Docker containers with their basic status on a remote EC2 instance via SSM.

    Returns container name, state (running/exited/restarting), and image.
    Use this first to get an overview of which containers are healthy
    and which ones have issues. Then use inspect_container for details
    on specific containers.
    """
    fmt = '{"name":"{{.Names}}","state":"{{.State}}","status":"{{.Status}}","image":"{{.Image}}"}'
    output = _ssm(instance_id, f"docker ps -a --format '{fmt}'")
    if output.startswith('{"error"'):
        return output

    containers = []
    for line in output.strip().split("\n"):
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    return json.dumps({"containers": containers, "total": len(containers)}, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def inspect_container(
    instance_id: Annotated[str, Field(description="EC2 instance ID to run the command on (e.g. 'i-0abc123def456').")],
    container_name: Annotated[str, Field(description="Name of the container to inspect.")],
) -> str:
    """Get detailed information about a specific Docker container on a remote EC2 instance via SSM.

    Returns state, exit code, restart count, start/finish times,
    environment variables, port mappings, and health check status.
    Use this after list_containers to dig into a specific container.
    """
    inspect_raw = _ssm(instance_id, f"docker inspect {container_name}")
    if inspect_raw.startswith('{"error"'):
        return inspect_raw

    try:
        info = json.loads(inspect_raw)[0]
        state = info.get("State", {})
        config = info.get("Config", {})
        network = info.get("NetworkSettings", {})
        host_config = info.get("HostConfig", {})

        ports = {}
        for container_port, bindings in (network.get("Ports") or {}).items():
            if bindings:
                ports[container_port] = [b.get("HostPort") for b in bindings]

        return json.dumps({
            "name": container_name,
            "image": config.get("Image", ""),
            "state": state.get("Status", "unknown"),
            "running": state.get("Running", False),
            "exit_code": state.get("ExitCode", None),
            "restart_count": info.get("RestartCount", 0),
            "started_at": state.get("StartedAt", ""),
            "finished_at": state.get("FinishedAt", ""),
            "env": config.get("Env", []),
            "ports": ports,
            "restart_policy": host_config.get("RestartPolicy", {}).get("Name", ""),
        }, indent=2)
    except (json.JSONDecodeError, IndexError) as e:
        return json.dumps({"error": f"Failed to parse inspect output: {e}"})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_container_logs(
    instance_id: Annotated[str, Field(description="EC2 instance ID to run the command on (e.g. 'i-0abc123def456').")],
    container_name: Annotated[str, Field(description="Name of the container to get logs from.")],
    tail_lines: Annotated[int, Field(description="Number of recent log lines to return.", ge=1, le=500)] = 100,
) -> str:
    """Get recent logs from a Docker container on a remote EC2 instance via SSM.

    Use this to find error messages, error codes, and failure details.
    Returns the most recent N lines from the container's stdout/stderr.
    For crash loop containers always use tail_lines=200 or more to capture
    the full error across multiple restart cycles.
    """
    # capture both stdout and stderr, include timestamps for timeline
    output = _ssm(instance_id, f"docker logs --tail {tail_lines} --timestamps {container_name} 2>&1")
    if output.startswith('{"error"'):
        return output

    lines = [l for l in output.strip().split("\n") if l]
    return json.dumps({
        "container": container_name,
        "lines_returned": len(lines),
        "logs": output,
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_active_alerts(
    severity: Annotated[str, Field(description="Filter by severity label (e.g. 'critical', 'warning'). Leave empty for all.")] = "",
    filter_label: Annotated[str, Field(description="Filter by any label as key=value (e.g. 'job=node', 'container=nginx'). Leave empty for no filter.")] = "",
) -> str:
    """Fetch all currently firing alerts from Alertmanager.

    Returns alert name, severity, labels, annotations, and how long it has been firing.
    Use this at the start of an investigation to get a full picture of what is broken right now.
    Then correlate with get_docker_events, get_container_logs, and query_metrics to find root cause.
    """
    try:
        url = f"{ALERTMANAGER_URL}/api/v2/alerts"
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code} from {e.request.url}"})
    except httpx.HTTPError as e:
        return json.dumps({"error": str(e)})

    # v2 API returns the array directly, v1 wrapped it in {"data": [...]}
    alerts = data if isinstance(data, list) else data.get("data", [])

    if severity:
        alerts = [a for a in alerts if a.get("labels", {}).get("severity", "").lower() == severity.lower()]
    if filter_label and "=" in filter_label:
        key, value = filter_label.split("=", 1)
        alerts = [a for a in alerts if a.get("labels", {}).get(key, "").lower() == value.lower()]

    parsed = []
    for a in alerts:
        parsed.append({
            "alert": a.get("labels", {}).get("alertname", "unknown"),
            "severity": a.get("labels", {}).get("severity", "unknown"),
            "state": a.get("status", {}).get("state", "unknown"),
            "active_since": a.get("startsAt", ""),
            "labels": a.get("labels", {}),
            "summary": a.get("annotations", {}).get("summary", ""),
            "description": a.get("annotations", {}).get("description", ""),
        })

    return json.dumps({
        "total_firing": len(parsed),
        "filters": {"severity": severity or "all", "label": filter_label or "none"},
        "alerts": parsed,
    }, indent=2)


def _prom(path: str, params: dict) -> str:
    try:
        resp = httpx.get(f"{PROM_URL}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return json.dumps(resp.json(), indent=2)
    except httpx.HTTPError as e:
        return json.dumps({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def query_metrics(
    query: Annotated[str, Field(description="PromQL instant query expression (e.g. 'up', 'rate(http_requests_total[5m])').")],
) -> str:
    """Run an instant PromQL query against Prometheus TSDB and return current values.

    Use this to fetch the current value of any metric or expression.
    For time-series data over a range, use query_metrics_range instead.
    """
    return _prom("/api/v1/query", {"query": query})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def query_metrics_range(
    query: Annotated[str, Field(description="PromQL range query expression.")],
    start: Annotated[str, Field(description="Start time as Unix timestamp or RFC3339 (e.g. '2024-01-01T00:00:00Z').")],
    end: Annotated[str, Field(description="End time as Unix timestamp or RFC3339.")],
    step: Annotated[str, Field(description="Query resolution step (e.g. '15s', '1m', '5m').")],
) -> str:
    """Run a PromQL range query against Prometheus TSDB and return time-series data.

    Use this to analyse trends, spikes, or degradation over a time window.
    For a single current value use query_metrics instead.
    """
    return _prom("/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_metrics(
    match: Annotated[str, Field(description="Optional metric name filter substring (e.g. 'http', 'container_cpu').")] = "",
) -> str:
    """List all metric names available in Prometheus TSDB.

    Use this when you don't know the exact metric name. Pass a filter
    substring in 'match' to narrow results, or leave empty to list all.
    """
    data = json.loads(_prom("/api/v1/label/__name__/values", {}))
    if "error" in data:
        return json.dumps(data)
    names = data.get("data", [])
    if match:
        names = [n for n in names if match.lower() in n.lower()]
    return json.dumps({"metrics": names, "total": len(names)}, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_metric_labels(
    metric_name: Annotated[str, Field(description="Exact metric name to fetch label names and values for.")],
) -> str:
    """Get all label names and their values for a specific metric in Prometheus TSDB.

    Use this to understand the dimensions available on a metric before
    writing a filtered PromQL query.
    """
    series = json.loads(_prom("/api/v1/series", {"match[]": metric_name}))
    if "error" in series:
        return json.dumps(series)

    label_map: dict[str, set] = {}
    for s in series.get("data", []):
        for k, v in s.items():
            label_map.setdefault(k, set()).add(v)

    return json.dumps({k: sorted(v) for k, v in label_map.items()}, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_container_disk_usage(
    instance_id: Annotated[str, Field(description="EC2 instance ID to run the command on (e.g. 'i-0abc123def456').")],
    container_name: Annotated[str, Field(description="Name of the container to check disk usage for.")],
) -> str:
    """Get disk usage for a specific Docker container on a remote EC2 instance via SSM.

    Returns two views:
    - writable_layer: the size of the container's writable layer from docker system df
    - filesystem: df -h output from inside the container showing mounted filesystem usage
    Use this to diagnose containers running out of disk space.
    """
    fs_raw = _ssm(instance_id, f"docker exec {container_name} df -h 2>&1")
    size_raw = _ssm(instance_id, f"docker inspect --size {container_name} --format '{{{{.SizeRootFs}}}} {{{{.SizeRw}}}}'")

    result = {
        "container": container_name,
        "writable_layer": {},
        "filesystem": fs_raw if not fs_raw.startswith('{"error"') else json.loads(fs_raw),
    }

    if not size_raw.startswith('{"error"'):
        parts = size_raw.strip().split()
        if len(parts) == 2:
            try:
                result["writable_layer"] = {
                    "size_root_fs_bytes": int(parts[0]),
                    "size_rw_bytes": int(parts[1]),
                    "size_root_fs_human": f"{int(parts[0]) / (1024**3):.2f} GB",
                    "size_rw_human": f"{int(parts[1]) / (1024**2):.2f} MB",
                }
            except ValueError:
                result["writable_layer"] = {"raw": size_raw.strip()}

    return json.dumps(result, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_docker_events(
    instance_id: Annotated[str, Field(description="EC2 instance ID to run the command on (e.g. 'i-0abc123def456').")],
    since: Annotated[str, Field(description="How far back to look for events (e.g. '1h', '30m', '2h').")] = "1h",
    container_name: Annotated[str, Field(description="Filter events for a specific container name. Leave empty for all containers.")] = "",
) -> str:
    """Get recent Docker events from a remote EC2 instance via SSM.

    Returns a timeline of container lifecycle events including OOM kills,
    crashes (exit code 137 = OOM), restarts, stops, starts, and image pulls.
    Use this to understand what happened and when, especially useful for
    correlating crashes with Prometheus metrics or log entries.
    """
    filter_flag = f"--filter container={container_name}" if container_name else ""
    cmd = f"docker events --since {since} --until 0s {filter_flag} --format '{{{{json .}}}}'"
    output = _ssm(instance_id, cmd)

    if output.startswith('{"error"'):
        return output

    events = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        try:
            event = json.loads(line)
            parsed = {
                "time": event.get("time", ""),
                "type": event.get("Type", ""),
                "action": event.get("Action", ""),
                "container": event.get("Actor", {}).get("Attributes", {}).get("name", event.get("id", "")[:12]),
                "image": event.get("Actor", {}).get("Attributes", {}).get("image", ""),
                "exit_code": event.get("Actor", {}).get("Attributes", {}).get("exitCode", None),
            }
            if parsed["action"] == "oom" or parsed.get("exit_code") == "137":
                parsed["oom_kill"] = True
            events.append(parsed)
        except json.JSONDecodeError:
            continue

    return json.dumps({
        "instance_id": instance_id,
        "since": since,
        "container_filter": container_name or "all",
        "total_events": len(events),
        "events": events,
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def debug_env() -> str:
    """Debug tool to check environment variables loaded in the MCP server."""
    return json.dumps({
        "PROMETHEUS_URL": os.environ.get("PROMETHEUS_URL", "NOT SET"),
        "ALERTMANAGER_URL": os.environ.get("ALERTMANAGER_URL", "NOT SET"),
        "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "NOT SET"),
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_host_diagnostics(
    instance_id: Annotated[str, Field(description="EC2 instance ID (e.g. 'i-0abc123def456').")],
) -> str:
    """Get host-level resource usage from a remote EC2 instance via SSM.

    Returns disk usage (df -h), memory usage (free -m), CPU load (uptime),
    top memory-consuming processes, and Docker daemon disk usage summary.
    Use this to understand overall host health before diving into containers.
    """
    commands = {
        "disk": "df -h --output=source,size,used,avail,pcent,target | head -20",
        "memory": "free -m",
        "cpu_load": "uptime",
        "top_processes": "ps aux --sort=-%mem | head -10 | awk '{print $1,$2,$3,$4,$11}'",
        "docker_disk": "docker system df",
    }
    results = {}
    for key, cmd in commands.items():
        out = _ssm(instance_id, cmd)
        results[key] = out if not out.startswith('{"error"') else json.loads(out)

    return json.dumps({"instance_id": instance_id, "diagnostics": results}, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def search_container_logs(
    instance_id: Annotated[str, Field(description="EC2 instance ID (e.g. 'i-0abc123def456').")],
    container_name: Annotated[str, Field(description="Name of the container to search logs in.")],
    pattern: Annotated[str, Field(description="Simple text string to search for in logs (e.g. 'Error', 'fatal', 'not defined', 'refused'). Use a single word or phrase, not regex.")],
    tail_lines: Annotated[int, Field(description="Number of recent log lines to search within.", ge=50, le=2000)] = 500,
) -> str:
    """Search a container's logs for a specific error pattern via SSM.

    Use this to find specific error messages, stack traces, or keywords
    without retrieving the full log. Returns matching lines with line numbers.
    """
    cmd = f"docker logs --tail {tail_lines} {container_name} 2>&1 | grep -E -n -i \"{pattern}\" | tail -50"
    output = _ssm(instance_id, cmd)
    if output.startswith('{"error"'):
        return output

    matches = [line for line in output.strip().split("\n") if line]
    return json.dumps({
        "container": container_name,
        "pattern": pattern,
        "match_count": len(matches),
        "matches": matches,
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_container_resource_usage(
    instance_id: Annotated[str, Field(description="EC2 instance ID (e.g. 'i-0abc123def456').")],
) -> str:
    """Get live CPU and memory usage for all running containers via SSM.

    Returns a snapshot of docker stats (no-stream) showing CPU%, memory usage,
    memory limit, and network I/O for each container.
    Use this to identify which container is consuming the most resources right now.
    """
    cmd = "docker stats --no-stream --format '{\"name\":\"{{.Name}}\",\"cpu\":\"{{.CPUPerc}}\",\"mem_usage\":\"{{.MemUsage}}\",\"mem_perc\":\"{{.MemPerc}}\",\"net_io\":\"{{.NetIO}}\",\"block_io\":\"{{.BlockIO}}\"}'"
    output = _ssm(instance_id, cmd)
    if output.startswith('{"error"'):
        return output

    containers = []
    for line in output.strip().split("\n"):
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                containers.append({"raw": line})

    return json.dumps({"instance_id": instance_id, "container_stats": containers}, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_host_oom_logs(
    instance_id: Annotated[str, Field(description="EC2 instance ID (e.g. 'i-0abc123def456').")],
) -> str:
    """Check kernel OOM killer logs on a remote EC2 instance via SSM.

    Reads dmesg for OOM kill events — the kernel always logs these even when
    the container produces no logs. Use this when a container has exit code 137
    or 9 and logs are empty, to confirm whether the kernel killed the process
    due to memory pressure.
    """
    output = _ssm(instance_id, "dmesg -T 2>/dev/null | grep -i 'oom\\|killed process\\|out of memory' | tail -30")
    if output.startswith('{"error"'):
        return output

    lines = [l for l in output.strip().split("\n") if l]
    return json.dumps({
        "instance_id": instance_id,
        "oom_events_found": len(lines),
        "oom_logs": lines if lines else ["No OOM events found in dmesg"],
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False))
def save_rca_report(
    instance_id: Annotated[str, Field(description="EC2 instance ID or IP that was investigated.")],
    alert_name: Annotated[str, Field(description="Name of the alert or incident (e.g. 'HostDiskFull', 'ContainerCrashLoop').")],
    severity: Annotated[str, Field(description="Severity of the incident: critical, warning, or info.")],
    timeline: Annotated[str, Field(description="Chronological sequence of events with exact timestamps from docker events or logs. Must include when it started and how it progressed.")],
    root_cause: Annotated[str, Field(description="MUST quote the exact log line, error message, or metric value that proves the root cause. Example: 'Container exited with code 1 due to: Error: APP_SECRET is not defined (from container logs)'. Never use vague language like 'likely' or 'possibly'.")],
    contributing_factors: Annotated[str, Field(description="Specific contributing factors with evidence. Example: 'APP_SECRET missing from docker-compose env section, injected env (0) from .env confirms no vars loaded'.")],
    impact: Annotated[str, Field(description="Specific services and endpoints affected. Example: 'backend API unavailable, frontend returns 502 on all /api/* routes'.")],
    immediate_fix: Annotated[str, Field(description="Exact shell commands to fix the issue right now. Example: 'docker stop backend && APP_SECRET=value docker compose up -d backend'. Never say investigate or check.")],
    long_term_fix: Annotated[str, Field(description="Specific config or code change to prevent recurrence. Example: 'Add APP_SECRET to docker-compose.yml env section and to .env.example with documentation'.")],
    metrics_evidence: Annotated[str, Field(description="Exact metric values or log lines as evidence. Example: 'disk at 94% on /, overlay2 layer 5.2GB, container exit code 1 repeated 11 times'.")] = "",
    topology_mermaid: Annotated[str, Field(description="Mermaid diagram string from get_service_topology showing service dependencies. Include if topology was retrieved.")] = "",
) -> str:
    """Save a structured Root Cause Analysis (RCA) report to a markdown file.

    Call this AFTER completing the full investigation — after you have gathered
    evidence from alerts, container logs, Docker events, and metrics.
    The report is saved to ./rca_reports/<alert_name>_<instance_id>_<timestamp>.md
    """
    import datetime
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_alert = alert_name.replace(" ", "_").replace("/", "-")
    safe_instance = instance_id.replace(".", "-")

    # Always save relative to server.py location regardless of working directory
    server_dir = os.path.dirname(os.path.abspath(__file__))
    reports_dir = os.path.join(server_dir, "rca_reports")
    os.makedirs(reports_dir, exist_ok=True)
    filename = os.path.join(reports_dir, f"{safe_alert}_{safe_instance}_{timestamp}.md")

    report = f"""# RCA Report: {alert_name}

**Instance:** {instance_id}
**Severity:** {severity}
**Generated:** {datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")}

---

## Timeline of Events
{timeline}

---

## Root Cause
{root_cause}

## Contributing Factors
{contributing_factors}

---

## Impact
{impact}

---

## Metrics Evidence
{metrics_evidence if metrics_evidence else "N/A"}

---

## Service Topology
{f"```mermaid{chr(10)}{topology_mermaid}{chr(10)}```" if topology_mermaid else "N/A"}

---

## Remediation

### Immediate Fix
{immediate_fix}

### Long-term Fix
{long_term_fix}

---
*Generated by SRE Agent*
"""
    with open(filename, "w") as f:
        f.write(report)

    return json.dumps({
        "status": "saved",
        "file": filename,
        "alert": alert_name,
        "instance": instance_id,
    }, indent=2)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_service_topology(
    instance_id: Annotated[str, Field(description="EC2 instance ID (e.g. 'i-0abc123def456').")],
    compose_paths: Annotated[str, Field(description="Comma-separated absolute paths to docker-compose files on the EC2 instance. e.g. '/opt/app/docker-compose.yml,/opt/db/docker-compose.yml'")],
) -> str:
    """Build full service topology from one or more docker-compose.yml files.

    For each compose file, extracts:
    - services and images
    - depends_on with conditions (service_healthy vs service_started)
    - env vars that reference other services
    - shared networks

    Then correlates with live docker ps to show:
    - which services are running / exited / restarting
    - which broken services are causing cascading failures (cascade_risk)
    - suggested investigation order (broken roots first)

    Use this at the START of every RCA investigation, before inspecting any container.
    Pass the mermaid field directly into save_rca_report as topology_mermaid.
    """
    import datetime
    import re

    DEP_KEYWORDS = ["URI", "URL", "HOST", "ADDR", "ENDPOINT", "DSN", "CONNECTION"]
    SKIP_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "::1", ""}

    def extract_host(val: str) -> str:
        val = val.strip()
        if "://" in val:
            val = val.split("://", 1)[1]
        if "@" in val:
            val = val.split("@", 1)[1]
        return val.split(":")[0].split("/")[0].lower().strip()

    def names_match(host: str, svc: str) -> bool:
        if not host or host in SKIP_HOSTS:
            return False
        svc_lower = svc.lower()
        segments = re.split(r"[-_]", svc_lower)
        return (
            host == svc_lower
            or host in segments
            or svc_lower.startswith(host)
            or host.startswith(svc_lower)
        )

    paths = [p.strip() for p in compose_paths.split(",") if p.strip()]

    # ── Step 1: Read and parse all compose files ──────────────────────────────
    all_services = {}  # svc_name → {image, depends_on, depends_on_conditions, networks, ports, env_deps, compose_file, restart, healthcheck}
    parse_errors = []

    for path in paths:
        raw = _ssm(instance_id, f"cat {path} 2>/dev/null || echo '__NOT_FOUND__'")

        if "__NOT_FOUND__" in raw or raw.startswith('{"error"'):
            parse_errors.append(f"Cannot read: {path}")
            continue

        try:
            import yaml
            doc = yaml.safe_load(raw) or {}
        except Exception as e:
            parse_errors.append(f"YAML parse error in {path}: {e}")
            continue

        services_block = doc.get("services") or {}

        for svc_name, svc_cfg in services_block.items():
            svc_cfg = svc_cfg or {}

            # depends_on — preserve conditions
            raw_deps = svc_cfg.get("depends_on", [])
            if isinstance(raw_deps, dict):
                depends_on = list(raw_deps.keys())
                depends_on_conditions = {
                    k: (v.get("condition", "service_started") if isinstance(v, dict) else "service_started")
                    for k, v in raw_deps.items()
                }
            elif isinstance(raw_deps, list):
                depends_on = raw_deps
                depends_on_conditions = {d: "service_started" for d in raw_deps}
            else:
                depends_on = []
                depends_on_conditions = {}

            # networks
            raw_nets = svc_cfg.get("networks")
            if isinstance(raw_nets, dict):
                networks = list(raw_nets.keys())
            elif isinstance(raw_nets, list):
                networks = raw_nets
            else:
                networks = ["default"]

            # ports
            ports = [str(p) for p in (svc_cfg.get("ports") or [])]

            # env vars — extract dependency signals
            raw_env = svc_cfg.get("environment") or []
            if isinstance(raw_env, dict):
                env_items = [f"{k}={v}" for k, v in raw_env.items() if v is not None]
            else:
                env_items = [str(e) for e in raw_env if "=" in str(e)]

            env_deps = []
            for e in env_items:
                key, val = e.split("=", 1)
                if any(kw in key.upper() for kw in DEP_KEYWORDS):
                    host = extract_host(val)
                    if host and host not in SKIP_HOSTS:
                        env_deps.append({"key": key, "val": val[:100], "resolved_host": host})

            # image or build context
            image = svc_cfg.get("image") or (
                f"<build:{svc_cfg['build']}" if isinstance(svc_cfg.get("build"), str)
                else f"<build:{svc_cfg.get('build', {}).get('context', '.')}>"
                if svc_cfg.get("build") else "<unknown>"
            )

            all_services[svc_name] = {
                "image": image,
                "depends_on": depends_on,
                "depends_on_conditions": depends_on_conditions,
                "networks": networks,
                "ports": ports,
                "env_deps": env_deps,
                "restart": svc_cfg.get("restart", "no"),
                "healthcheck": bool(svc_cfg.get("healthcheck")),
                "compose_file": path,
            }

    if not all_services:
        return json.dumps({
            "error": "No services found in any compose file",
            "paths_tried": paths,
            "parse_errors": parse_errors,
        })

    # ── Step 2: Get live container state ──────────────────────────────────────
    list_raw = _ssm(
        instance_id,
        """docker ps -a --format '{"name":"{{.Names}}","state":"{{.State}}","status":"{{.Status}}"}'"""
    )

    running_containers = {}
    if not list_raw.startswith('{"error"'):
        for line in list_raw.strip().split("\n"):
            if not line:
                continue
            try:
                c = json.loads(line)
                running_containers[c["name"]] = {"state": c["state"], "status": c["status"]}
            except json.JSONDecodeError:
                continue

    def find_container(svc_name: str) -> dict:
        # 1. exact match
        if svc_name in running_containers:
            return {"container_name": svc_name, **running_containers[svc_name]}
        # 2. segment match — handles project_svc_1 and project-svc-1
        for cname, cinfo in running_containers.items():
            segs = re.split(r"[-_]", cname.lower())
            if svc_name.lower() in segs:
                return {"container_name": cname, **cinfo}
        return {"container_name": None, "state": "not_found", "status": "no matching container"}

    service_health = {svc: find_container(svc) for svc in all_services}

    # ── Step 3: Build edges ───────────────────────────────────────────────────
    edges = []
    service_names = set(all_services.keys())

    # Signal 1 — depends_on (highest confidence)
    for svc, info in all_services.items():
        for dep in info["depends_on"]:
            condition = info["depends_on_conditions"].get(dep, "service_started")
            edges.append({
                "from": svc,
                "to": dep,
                "type": "depends_on",
                "condition": condition,
                "confidence": "high",
                "reason": f"depends_on (condition: {condition})",
                "compose_file": info["compose_file"],
            })

    # Signal 2 — env var references
    for svc, info in all_services.items():
        for dep in info["env_deps"]:
            host = dep["resolved_host"]
            for other in service_names:
                if other == svc:
                    continue
                if names_match(host, other):
                    # skip if already covered by depends_on
                    if not any(e["from"] == svc and e["to"] == other and e["type"] == "depends_on" for e in edges):
                        edges.append({
                            "from": svc,
                            "to": other,
                            "type": "env_ref",
                            "condition": None,
                            "confidence": "high",
                            "reason": f"env {dep['key']}={dep['val'][:50]}",
                            "compose_file": info["compose_file"],
                        })

    # ── Step 4: Broken services + cascade risk ────────────────────────────────
    broken = [
        {
            "service": svc,
            "container": health["container_name"],
            "state": health["state"],
            "status": health["status"],
            "image": all_services[svc]["image"],
            "restart_policy": all_services[svc]["restart"],
            "compose_file": all_services[svc]["compose_file"],
        }
        for svc, health in service_health.items()
        if health["state"] not in ("running",)
    ]

    broken_names = {b["service"] for b in broken}

    cascade_risk = []
    for e in edges:
        if e["to"] in broken_names and e["confidence"] == "high":
            h = service_health.get(e["from"], {})
            cascade_risk.append({
                "service": e["from"],
                "depends_on_broken": e["to"],
                "service_state": h.get("state", "unknown"),
                "dependency_type": e["type"],
                "condition": e.get("condition"),
                "risk_level": "confirmed" if h.get("state") != "running" else "at_risk",
            })

    # ── Step 5: Investigation order (topological — roots first) ───────────────
    incoming = {svc: 0 for svc in all_services}
    for e in edges:
        if e["to"] in incoming:
            incoming[e["to"]] += 1

    root_services = [s for s, n in incoming.items() if n == 0]
    broken_roots = [s for s in root_services if s in broken_names]

    seen_o: set = set()
    investigation_order = []
    for s in (
        broken_roots
        + [b["service"] for b in broken if b["service"] not in broken_roots]
        + [c["service"] for c in cascade_risk if c["service"] not in broken_names]
    ):
        if s not in seen_o:
            seen_o.add(s)
            investigation_order.append(s)

    # ── Step 6: Mermaid diagram ───────────────────────────────────────────────
    def safe_id(n: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", n)

    mermaid_lines = ["graph LR"]
    for svc in broken_names:
        mermaid_lines.append(f'  style {safe_id(svc)} fill:#ff4444,color:#fff')
    for c in cascade_risk:
        if c["service"] not in broken_names:
            mermaid_lines.append(f'  style {safe_id(c["service"])} fill:#ff9900,color:#fff')

    arrow = {"depends_on": "==>", "env_ref": "-->"}
    for e in edges:
        arr = arrow.get(e["type"], "-->")
        label = e.get("condition") or e["type"]
        mermaid_lines.append(
            f'  {safe_id(e["from"])}["{e["from"]}"] {arr}|{label}| {safe_id(e["to"])}["{e["to"]}"]'
        )

    mermaid = "\n".join(mermaid_lines)

    # ── Step 7: Save topology file ────────────────────────────────────────────
    server_dir = os.path.dirname(os.path.abspath(__file__))
    topology_dir = os.path.join(server_dir, "topology")
    os.makedirs(topology_dir, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    topology_file = os.path.join(topology_dir, f"topology_{instance_id}_{ts}.md")

    with open(topology_file, "w") as f:
        f.write(f"# Service Topology: {instance_id}\n\n")
        f.write(f"**Compose files:** {', '.join(paths)}\n")
        f.write(f"**Generated:** {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
        f.write("```mermaid\n" + mermaid + "\n```\n\n")
        f.write("## Services\n\n")
        for svc, info in all_services.items():
            h = service_health[svc]
            icon = "🟢" if h["state"] == "running" else "🔴"
            f.write(f"### {icon} {svc}\n")
            f.write(f"- Image: {info['image']}\n")
            f.write(f"- State: {h['state']} ({h['status']})\n")
            f.write(f"- Container: {h['container_name'] or 'not found'}\n")
            f.write(f"- Ports: {', '.join(info['ports']) or 'none'}\n")
            f.write(f"- depends_on: {', '.join(info['depends_on']) or 'none'}\n")
            f.write(f"- Restart: {info['restart']}\n")
            f.write(f"- Healthcheck defined: {info['healthcheck']}\n\n")
        f.write("## Broken Services\n\n")
        for b in broken:
            f.write(f"- 🔴 **{b['service']}** state={b['state']} container={b['container']}\n")
        f.write("\n## Cascade Risk\n\n")
        for c in cascade_risk:
            f.write(f"- ⚠️ **{c['service']}** → depends on broken **{c['depends_on_broken']}** "
                    f"(type={c['dependency_type']}, risk={c['risk_level']})\n")
        f.write("\n## Investigation Order\n\n")
        for i, s in enumerate(investigation_order, 1):
            f.write(f"{i}. {s}\n")
        if parse_errors:
            f.write("\n## Parse Errors\n\n")
            for err in parse_errors:
                f.write(f"- {err}\n")

    return json.dumps({
        "instance_id": instance_id,
        "compose_files_read": paths,
        "parse_errors": parse_errors,
        "services": {
            svc: {
                "image": info["image"],
                "depends_on": info["depends_on"],
                "depends_on_conditions": info["depends_on_conditions"],
                "ports": info["ports"],
                "networks": info["networks"],
                "restart": info["restart"],
                "healthcheck": info["healthcheck"],
                "compose_file": info["compose_file"],
                "live_state": service_health[svc]["state"],
                "live_container": service_health[svc]["container_name"],
                "live_status": service_health[svc]["status"],
            }
            for svc, info in all_services.items()
        },
        "edges": edges,
        "broken": broken,
        "cascade_risk": cascade_risk,
        "root_services": root_services,
        "broken_roots": broken_roots,
        "investigation_order": investigation_order,
        "mermaid": mermaid,
        "topology_file": topology_file,
        "summary": {
            "total_services": len(all_services),
            "broken_count": len(broken),
            "cascade_risk_count": len(cascade_risk),
            "broken_roots": broken_roots,
            "total_edges": len(edges),
        },
    }, indent=2)
if __name__ == '__main__':
    mcp.run()
