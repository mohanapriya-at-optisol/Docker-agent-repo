"""
Webhook server that receives Alertmanager alerts and triggers the SRE agent automatically.

Alertmanager sends a POST to /webhook when an alert fires.
This server extracts the instance ID and compose path, then runs the agent.

Usage:
    python webhook_server.py

Configure Alertmanager to send to:
    http://<this-machine-ip>:9099/webhook
"""

import asyncio
import json
import logging
import os
from datetime import datetime

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

# Import the root_agent from agent.py
from agent import root_agent

# Compose file path on the EC2 instance — update this to match your setup
COMPOSE_PATH = os.environ.get("COMPOSE_PATH", "/home/ubuntu/mern-app-testing/docker-compose.yml")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "your-username/mern-app-testing"

APP_NAME = "sre-webhook"
session_service = InMemorySessionService()


async def run_investigation(alert_name: str, instance_id: str, severity: str, summary: str):
    """Run the SRE agent investigation for a fired alert."""
    session_id = f"webhook-{alert_name}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    user_id = "alertmanager"

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

    repo_context = f"GitHub repo is {GITHUB_REPO}. " if GITHUB_REPO else ""
    message = (
        f"Alert '{alert_name}' fired with severity '{severity}' on instance {instance_id}. "
        f"Summary: {summary}. "
        f"Compose file is at {COMPOSE_PATH}. "
        f"{repo_context}"
        f"Please investigate and prepare a full RCA report."
    )

    log.info(f"Starting investigation for alert={alert_name} instance={instance_id}")

    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=Content(role="user", parts=[Part(text=message)]),
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if part.text:
                        log.info(f"Investigation complete for {alert_name}:\n{part.text}")
    except Exception as e:
        log.error(f"Investigation failed for {alert_name}: {e}")


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
            log.warning(f"Alert {alert_name} has no valid instance_id in labels: {labels}")
            # Still trigger with whatever we have
            instance_id = instance_id or "unknown"

        log.info(f"Triggering investigation: alert={alert_name} instance={instance_id} severity={severity}")

        background_tasks.add_task(
            run_investigation,
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


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9099)
