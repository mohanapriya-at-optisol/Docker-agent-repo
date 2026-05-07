"""
Webhook server that receives Alertmanager/Grafana alerts and triggers the SRE agent.

When an alert fires:
1. Webhook receives the alert
2. Posts a Teams Adaptive Card asking for missing inputs (compose path, Jenkins job, GitHub repo)
3. User fills in the card and submits
4. /submit endpoint receives the inputs and starts the investigation

Usage:
    python webhook_server.py

Endpoints:
    POST /webhook  — receives alerts from Alertmanager or Grafana
    POST /submit   — receives user inputs from Teams card
    GET  /health   — health check
"""

import asyncio
import json
import logging
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="SRE Agent Webhook")

from agent import root_agent

COMPOSE_PATH = os.environ.get("COMPOSE_PATH", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
JENKINS_JOB_NAME = os.environ.get("JENKINS_JOB_NAME", "")
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")

# Load instance registry from SQLite
from setup_db import resolve_ip_to_instance, get_apps_for_instance

def get_app_by_container(instance_id: str, container_name: str) -> dict:
    """Find the app registration that matches a container name."""
    apps = get_apps_for_instance(instance_id)
    container_lower = container_name.lower()
    for app in apps:
        app_name_lower = app["app_name"].lower()
        # Match if container name contains the app name
        if app_name_lower in container_lower or container_lower in app_name_lower:
            return app
    # Return first app if only one registered
    return apps[0] if len(apps) == 1 else {}


def lookup_instance(instance_id: str, ip: str = "") -> dict:
    """Look up app context for an instance from the SQLite registry."""
    # Try IP → instance_id via DB if not already resolved
    if ip and not instance_id.startswith("i-"):
        db_instance = resolve_ip_to_instance(ip)
        if db_instance:
            instance_id = db_instance

    apps = get_apps_for_instance(instance_id)

    if len(apps) == 1:
        return {"instance_id": instance_id, "app": apps[0], "multiple": False}
    elif len(apps) > 1:
        return {"instance_id": instance_id, "apps": apps, "multiple": True}
    return {"instance_id": instance_id, "app": None, "multiple": False}

APP_NAME = "sre-webhook"
session_service = InMemorySessionService()


# Track active investigations to avoid duplicates
active_investigations: set = set()
# Global lock — only one terminal prompt at a time
_prompt_lock = asyncio.Lock()


async def prompt_and_investigate(alert_name: str, instance_id: str, severity: str, summary: str):
    """Look up all apps for the instance and let the agent figure out which one is affected."""
    key = f"{alert_name}:{instance_id}"
    if key in active_investigations:
        log.info(f"Investigation already running for {key} — skipping duplicate")
        return
    active_investigations.add(key)

    try:
        async with _prompt_lock:
            registry_result = lookup_instance(instance_id)
            apps = registry_result.get("apps") or ([registry_result["app"]] if registry_result.get("app") else [])

            print(f"\n{'='*60}")
            print(f"🚨 ALERT RECEIVED: {alert_name}")
            print(f"   Instance: {instance_id} | Severity: {severity}")
            print(f"   Summary: {summary}")
            if apps:
                print(f"   Apps in registry: {', '.join(a['app_name'] for a in apps)}")
            print(f"{'='*60}")

            loop = asyncio.get_event_loop()

            def prompt_with_default(prompt_text: str, default: str) -> str:
                display = f"{prompt_text} [{default}]: " if default else f"{prompt_text}: "
                val = input(display).strip()
                return val if val else default

            # Only prompt if nothing is in the registry at all
            if not apps:
                compose_path = await loop.run_in_executor(
                    None, prompt_with_default,
                    "Docker-compose path on EC2",
                    COMPOSE_PATH or "/home/ubuntu/myapp/docker-compose.yml"
                )
                github_repo = await loop.run_in_executor(
                    None, prompt_with_default,
                    "GitHub repo (owner/repo, Enter to skip)",
                    GITHUB_REPO
                )
                jenkins_job = await loop.run_in_executor(
                    None, prompt_with_default,
                    "Jenkins job name (Enter to skip)",
                    JENKINS_JOB_NAME
                )
                apps = [{"app_name": "unknown", "compose_path": compose_path,
                         "github_repo": github_repo, "jenkins_job": jenkins_job}]

            print(f"\n▶ Starting investigation — agent will identify the affected service...\n")

        await run_investigation(
            alert_name=alert_name,
            instance_id=instance_id,
            severity=severity,
            summary=summary,
            apps=apps,
        )
    finally:
        active_investigations.discard(key)


async def run_investigation(alert_name: str, instance_id: str, severity: str, summary: str,
                             apps: list = None, compose_path: str = "", github_repo: str = "", jenkins_job: str = ""):
    """Run the SRE agent investigation for a fired alert."""
    session_id = f"webhook-{alert_name}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    user_id = "alertmanager"

    # Build app context from apps list or fallback values
    if apps:
        all_compose_paths = ",".join(a["compose_path"] for a in apps)
        apps_context = "\n".join([
            f"  - App '{a['app_name']}': compose={a['compose_path']}, "
            f"repo={a.get('github_repo','N/A')}, jenkins={a.get('jenkins_job','N/A')}"
            for a in apps
        ])
        app_section = (
            f"The following applications are deployed on this instance:\n{apps_context}\n"
            f"Call get_service_topology with compose_paths='{all_compose_paths}' to map all services. "
            f"Then identify the faulty container from logs and match it to the correct app above "
            f"to get the right GitHub repo and Jenkins job."
        )
        compose = apps[0]["compose_path"] if apps else (compose_path or COMPOSE_PATH)
    else:
        compose = compose_path or COMPOSE_PATH
        repo = github_repo or GITHUB_REPO
        jenkins = jenkins_job or JENKINS_JOB_NAME
        app_section = (
            f"Compose file is at {compose}. "
            f"{'GitHub repo is ' + repo + '. ' if repo else ''}"
            f"{'Jenkins job is ' + jenkins + '. ' if jenkins else ''}"
        )

    message = (
        f"Alert '{alert_name}' fired with severity '{severity}' on instance {instance_id}. "
        f"Summary: {summary}. "
        f"{app_section}"
        f"Do NOT ask the user which service is affected — investigate the containers yourself "
        f"to identify the faulty service, then use the matching app context above. "
        f"Start the investigation immediately."
    )

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )

    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    print(f"\n{'─'*60}")
    print(f"🔍 INVESTIGATION STARTED")
    print(f"   Alert:    {alert_name}")
    print(f"   Instance: {instance_id}")
    print(f"   Severity: {severity}")
    print(f"{'─'*60}\n")

    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=Content(role="user", parts=[Part(text=message)]),
        ):
            # Tool call — agent is about to call a tool
            if hasattr(event, 'content') and event.content:
                for part in event.content.parts:
                    # Agent reasoning / KNOW-UNKNOWN-CALLING text
                    if hasattr(part, 'text') and part.text:
                        if not event.is_final_response():
                            print(f"\n🤔 {part.text.strip()}")

                    # Tool call being made
                    if hasattr(part, 'function_call') and part.function_call:
                        fc = part.function_call
                        args = dict(fc.args) if fc.args else {}
                        # Show key args only, truncate long values
                        short_args = {k: (str(v)[:80] + "...") if len(str(v)) > 80 else v
                                      for k, v in args.items()}
                        print(f"\n🔧 CALLING: {fc.name}")
                        for k, v in short_args.items():
                            print(f"   {k}: {v}")

                    # Tool response received
                    if hasattr(part, 'function_response') and part.function_response:
                        fr = part.function_response
                        response_str = str(fr.response)
                        preview = response_str[:300] + "..." if len(response_str) > 300 else response_str
                        print(f"\n✅ RESULT: {fr.name}")
                        print(f"   {preview}")

            # Final response from agent
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if hasattr(part, 'text') and part.text:
                        print(f"\n{'─'*60}")
                        print(f"📋 INVESTIGATION COMPLETE")
                        print(f"{'─'*60}")
                        print(part.text)
                        print(f"{'─'*60}\n")

    except Exception as e:
        log.error(f"Investigation failed for {alert_name}: {e}")
        print(f"\n❌ Investigation failed: {e}\n")


@app.post("/webhook")
async def alertmanager_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive Alertmanager webhook and trigger agent investigation."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    alerts = payload.get("alerts", [])
    log.info(f"Received {len(alerts)} alert(s) from Alertmanager")

    triggered = []
    for alert in alerts:
        # Only process firing alerts
        if alert.get("status") != "firing":
            continue

        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})

        alert_name = labels.get("alertname", "UnknownAlert")
        severity = labels.get("severity", "unknown")
        summary = annotations.get("summary", "No summary provided")

        # Extract instance ID from labels — try common label names
        instance_id = (
            labels.get("instance_id") or
            labels.get("ec2_instance_id") or
            labels.get("instance", "").split(":")[0]  # strip port if present
        )

        if not instance_id or not instance_id.startswith("i-"):
            # Try to resolve IP to instance ID via AWS EC2 API
            ip = instance_id or labels.get("instance", "").split(":")[0]
            if ip:
                try:
                    import boto3
                    ec2 = boto3.client("ec2")
                    for filter_name in ["ip-address", "private-ip-address"]:
                        resp = ec2.describe_instances(
                            Filters=[{"Name": filter_name, "Values": [ip]}]
                        )
                        reservations = resp.get("Reservations", [])
                        if reservations:
                            instance_id = reservations[0]["Instances"][0]["InstanceId"]
                            log.info(f"Resolved IP {ip} to instance_id {instance_id}")
                            break
                except Exception as e:
                    log.warning(f"Could not resolve IP {ip} to instance_id: {e}")

            if not instance_id or not instance_id.startswith("i-"):
                log.warning(f"Alert {alert_name} has no valid instance_id in labels: {labels}")
                instance_id = instance_id or "unknown"

        # Skip Grafana internal alerts that have no actionable instance
        if alert_name in ("DatasourceNoData", "DatasourceError", "DatasourceNotFound"):
            log.info(f"Skipping Grafana internal alert: {alert_name}")
            continue

        if not instance_id or instance_id == "unknown":
            log.warning(f"Skipping alert {alert_name} — no valid instance_id found in labels: {labels}")
            continue

        log.info(f"Triggering investigation: alert={alert_name} instance={instance_id} severity={severity}")

        # Ask for missing inputs in the terminal
        background_tasks.add_task(
            prompt_and_investigate,
            alert_name=alert_name,
            instance_id=instance_id,
            severity=severity,
            summary=summary,
        )

        triggered.append({"alert": alert_name, "instance": instance_id})

    return JSONResponse({
        "status": "accepted",
        "investigations_triggered": len(triggered),
        "alerts": triggered,
    })


@app.post("/teams")
async def teams_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive a trigger from a Teams Workflow and start investigation."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    instance_id = payload.get("instance_id", "")
    alert_name = payload.get("alert_name", "TeamsAlert")
    severity = payload.get("severity", "critical")
    summary = payload.get("summary", "")

    # Try to extract instance IP from the message text if instance_id not provided
    if not instance_id and summary:
        import re
        # Match patterns like (15.207.18.201:9100 or instance = 15.207.18.201:9100
        ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):\d+', summary)
        if ip_match:
            ip = ip_match.group(1)
            log.info(f"Extracted IP {ip} from Teams message — instance_id must be provided or mapped")
            summary = f"{summary}\n\nExtracted IP: {ip} — please provide instance_id for SSM access"

    # Extract alert name from message if not provided
    if alert_name == "TeamsAlert" and summary:
        import re
        name_match = re.search(r'\[FIRING:\d+\]\s+([^\(]+)', summary)
        if name_match:
            alert_name = name_match.group(1).strip()

    if not instance_id:
        return JSONResponse({
            "error": "instance_id is required. Add it to the workflow body or map the IP to an instance ID.",
            "tip": "Add instance_id to your Power Automate HTTP body"
        }, status_code=400)

    log.info(f"Teams trigger: alert={alert_name} instance={instance_id}")
    background_tasks.add_task(
        run_investigation,
        alert_name=alert_name,
        instance_id=instance_id,
        severity=severity,
        summary=summary,
    )

    return JSONResponse({
        "status": "accepted",
        "message": f"Investigation started for {alert_name} on {instance_id}",
    })


@app.post("/register")
async def register_app(request: Request):
    """Register or update an app deployment in the registry.

    Called automatically by Jenkins after a successful deployment.
    Body: { instance_id, app_name, compose_path, github_repo, jenkins_job }
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    instance_id = payload.get("instance_id", "").strip()
    app_name = payload.get("app_name", "").strip()
    compose_path = payload.get("compose_path", "").strip()
    github_repo = payload.get("github_repo", "").strip()
    jenkins_job = payload.get("jenkins_job", "").strip()

    if not instance_id or not app_name or not compose_path:
        return JSONResponse({"error": "instance_id, app_name, compose_path are required"}, status_code=400)

    try:
        from setup_db import get_conn
        conn = get_conn()
        # Upsert instance
        conn.execute(
            "INSERT OR IGNORE INTO instances (instance_id, name) VALUES (?, ?)",
            (instance_id, instance_id)
        )
        # Upsert app — always update to latest values
        existing = conn.execute(
            "SELECT id FROM apps WHERE instance_id=? AND app_name=?",
            (instance_id, app_name)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE apps SET compose_path=?, github_repo=?, jenkins_job=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (compose_path, github_repo, jenkins_job, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO apps (instance_id, app_name, compose_path, github_repo, jenkins_job) VALUES (?,?,?,?,?)",
                (instance_id, app_name, compose_path, github_repo, jenkins_job)
            )
        # Always write deployment history
        conn.execute(
            "INSERT INTO deployment_history (instance_id, app_name, compose_path, github_repo, jenkins_job) VALUES (?,?,?,?,?)",
            (instance_id, app_name, compose_path, github_repo, jenkins_job)
        )
        conn.commit()
        conn.close()
        log.info(f"Registered app '{app_name}' on {instance_id}")
        return JSONResponse({"status": "registered", "app": app_name, "instance": instance_id})
    except Exception as e:
        log.error(f"Registration failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok", "active_investigations": len(active_investigations)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9099)
