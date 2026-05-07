"""
Jenkins MCP server — exposes Jenkins build data to the SRE agent.

Tools:
- get_jenkins_jobs        : list all jobs and their last build status
- get_last_build          : last build details for a specific job
- get_build_changes       : commits included in a specific build
- get_build_console       : console output of a build
- get_builds_since        : all builds that ran after a given timestamp

Configure via .env:
    JENKINS_URL=http://jenkins.yourcompany.com:8080
    JENKINS_USER=your_username
    JENKINS_API_TOKEN=your_api_token
"""

import json
import os
from typing import Annotated

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

mcp = FastMCP("jenkins-agent")

JENKINS_URL = os.environ.get("JENKINS_URL", "").rstrip("/")
JENKINS_USER = os.environ.get("JENKINS_USER", "")
JENKINS_API_TOKEN = os.environ.get("JENKINS_API_TOKEN", "")


def _jenkins(path: str, params: dict = None, text: bool = False):
    if not JENKINS_URL:
        return {"error": "JENKINS_URL not configured in .env"}
    auth = (JENKINS_USER, JENKINS_API_TOKEN) if JENKINS_USER and JENKINS_API_TOKEN else None
    try:
        resp = httpx.get(
            f"{JENKINS_URL}{path}",
            auth=auth,
            params=params or {},
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text if text else resp.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except httpx.HTTPError as e:
        return {"error": str(e)}


def _fmt_timestamp(ms: int) -> str:
    """Convert Jenkins millisecond timestamp to ISO string."""
    if not ms:
        return ""
    import datetime
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_jenkins_jobs() -> str:
    """List all Jenkins jobs with their last build status and timestamp.

    Use this to get an overview of all pipelines and find which job
    deployed to the affected instance around the incident time.
    """
    data = _jenkins("/api/json", {
        "tree": "jobs[name,lastBuild[number,result,timestamp,duration,url]]"
    })
    if "error" in data:
        return json.dumps(data)

    jobs = []
    for job in data.get("jobs", []):
        lb = job.get("lastBuild") or {}
        jobs.append({
            "name": job["name"],
            "last_build": lb.get("number"),
            "result": lb.get("result", "unknown"),
            "timestamp": _fmt_timestamp(lb.get("timestamp", 0)),
            "duration_sec": round(lb.get("duration", 0) / 1000),
        })

    return json.dumps({"total_jobs": len(jobs), "jobs": jobs}, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_last_build(
    job_name: Annotated[str, Field(description="Jenkins job name (e.g. 'deploy-backend', 'mern-app-deploy').")],
) -> str:
    """Get details of the last build for a Jenkins job.

    Returns build number, result, timestamp, duration, triggered by (user or SCM/GitHub webhook),
    and the commit SHA that triggered the build.

    Use this to find what was deployed and when — cross-reference the build timestamp
    with the incident start time from docker events.
    """
    data = _jenkins(f"/job/{job_name}/lastBuild/api/json")
    if "error" in data:
        return json.dumps(data)

    # Extract trigger cause
    causes = []
    for action in data.get("actions", []):
        for cause in action.get("causes", []):
            causes.append(cause.get("shortDescription", ""))

    # Extract commit SHA from SCM actions
    commit_sha = ""
    branch = ""
    for action in data.get("actions", []):
        for revision in action.get("buildsByBranchName", {}).values():
            commit_sha = revision.get("revision", {}).get("SHA1", "")[:12]
        for branch_info in action.get("branches", []):
            branch = branch_info.get("name", "")

    return json.dumps({
        "job": job_name,
        "build_number": data.get("number"),
        "result": data.get("result", "IN_PROGRESS"),
        "timestamp": _fmt_timestamp(data.get("timestamp", 0)),
        "duration_sec": round(data.get("duration", 0) / 1000),
        "triggered_by": causes,
        "commit_sha": commit_sha,
        "branch": branch,
        "url": data.get("url", ""),
        "description": data.get("description", ""),
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_build_changes(
    job_name: Annotated[str, Field(description="Jenkins job name.")],
    build_number: Annotated[int, Field(description="Build number. Use -1 for the last build.")],
) -> str:
    """Get the list of commits included in a specific Jenkins build.

    Returns commit SHA, author, message, and changed files for each commit.
    Use this after get_last_build to see exactly what code was deployed.
    Cross-reference commit SHA with get_commit_diff in the GitHub MCP server.
    """
    path = f"/job/{job_name}/lastBuild/api/json" if build_number == -1 else f"/job/{job_name}/{build_number}/api/json"
    data = _jenkins(path, {"tree": "changeSet[items[commitId,author[fullName],msg,paths[editType,file]]]"})
    if "error" in data:
        return json.dumps(data)

    change_set = data.get("changeSet", {})
    items = change_set.get("items", [])

    commits = []
    for item in items:
        commits.append({
            "sha": item.get("commitId", "")[:12],
            "full_sha": item.get("commitId", ""),
            "author": item.get("author", {}).get("fullName", "unknown"),
            "message": item.get("msg", ""),
            "files_changed": [
                {"file": p.get("file"), "change_type": p.get("editType")}
                for p in item.get("paths", [])
            ],
        })

    return json.dumps({
        "job": job_name,
        "build_number": build_number if build_number != -1 else "last",
        "total_commits": len(commits),
        "commits": commits,
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_build_console(
    job_name: Annotated[str, Field(description="Jenkins job name.")],
    build_number: Annotated[int, Field(description="Build number. Use -1 for the last build.")],
    tail_lines: Annotated[int, Field(description="Number of lines from the end of the console output.", ge=10, le=200)] = 50,
) -> str:
    """Get the console output of a Jenkins build.

    Use this when a build failed and you need to see the error output,
    or to confirm what docker commands were run during deployment.
    Returns the last N lines of the console log.
    """
    path = f"/job/{job_name}/lastBuild/consoleText" if build_number == -1 else f"/job/{job_name}/{build_number}/consoleText"
    text = _jenkins(path, text=True)
    if isinstance(text, dict) and "error" in text:
        return json.dumps(text)

    lines = text.strip().split("\n")
    tail = lines[-tail_lines:] if len(lines) > tail_lines else lines

    return json.dumps({
        "job": job_name,
        "build_number": build_number if build_number != -1 else "last",
        "total_lines": len(lines),
        "showing_last": len(tail),
        "console": "\n".join(tail),
    }, indent=2)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_builds_since(
    job_name: Annotated[str, Field(description="Jenkins job name.")],
    since_timestamp: Annotated[str, Field(description="ISO timestamp to find builds after (e.g. '2026-04-30T14:00:00Z').")],
) -> str:
    """Get all builds of a Jenkins job that ran after a specific timestamp.

    Use this to find all deployments that happened in the window before an incident.
    Pass the incident start time to find which builds could have caused it.
    """
    import datetime

    data = _jenkins(f"/job/{job_name}/api/json", {
        "tree": "builds[number,result,timestamp,duration,url]"
    })
    if "error" in data:
        return json.dumps(data)

    try:
        since_dt = datetime.datetime.fromisoformat(since_timestamp.replace("Z", "+00:00"))
        since_ms = int(since_dt.timestamp() * 1000)
    except ValueError:
        return json.dumps({"error": f"Invalid timestamp format: {since_timestamp}. Use ISO format like 2026-04-30T14:00:00Z"})

    matching = []
    for build in data.get("builds", []):
        if build.get("timestamp", 0) >= since_ms:
            matching.append({
                "build_number": build["number"],
                "result": build.get("result", "IN_PROGRESS"),
                "timestamp": _fmt_timestamp(build.get("timestamp", 0)),
                "duration_sec": round(build.get("duration", 0) / 1000),
            })

    return json.dumps({
        "job": job_name,
        "since": since_timestamp,
        "builds_found": len(matching),
        "builds": matching,
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
