# Setup Instructions

Follow these steps in order. Each step builds on the previous one.

---

## What you need before starting

- A computer running Linux or Mac
- Python 3.11 or newer installed
- An AWS account with EC2 instances running Docker containers
- A Gemini API key (free at https://aistudio.google.com)
- AWS access keys with SSM permissions

---

## Step 1 — Download the project

Open a terminal and run:

```bash
git clone <your-repo-url>
cd docker-mcp-agent
```

---

## Step 2 — Create a Python virtual environment

This keeps the project's dependencies separate from your system Python.

```bash
python3 -m venv venv
source venv/bin/activate
```

You should see `(venv)` appear at the start of your terminal prompt. This means it worked.

---

## Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

Wait for it to finish. This installs all the libraries the agent needs.

---

## Step 4 — Fill in your credentials

Open the file `sre_agent/.env` in any text editor and fill in your values:

```
GEMINI_API_KEY=paste_your_gemini_key_here

AWS_ACCESS_KEY_ID=paste_your_aws_access_key_here
AWS_SECRET_ACCESS_KEY=paste_your_aws_secret_key_here
AWS_DEFAULT_REGION=ap-south-1

PROMETHEUS_URL=http://your-prometheus-ip:9090
ALERTMANAGER_URL=http://your-alertmanager-ip:9093
```

**Where to get these:**

- Gemini API key: https://aistudio.google.com → Get API key
- AWS keys: AWS Console → IAM → Users → Your user → Security credentials → Create access key
- Prometheus/Alertmanager URLs: the IP address of your monitoring server, usually port 9090 and 9093

> **Important:** Never share this file or commit it to git. It contains secrets.

---

## Step 5 — Check AWS SSM is set up on your EC2 instances

The agent connects to your EC2 instances using AWS SSM (no SSH needed). For this to work:

1. Your EC2 instances must have the **SSM Agent** installed and running
   - Amazon Linux 2 and Ubuntu 20.04+ have it pre-installed
   - Check: `sudo systemctl status amazon-ssm-agent`

2. Your EC2 instances must have an **IAM role** with the `AmazonSSMManagedInstanceCore` policy attached

3. Your AWS access keys must have the `AmazonSSMFullAccess` policy (or equivalent)

If you're unsure, ask your AWS administrator to verify these three things.

---

## Step 6 — Start the agent

Make sure your virtual environment is active (you see `(venv)` in the terminal), then run:

```bash
adk web /home/your-username/path-to/docker-mcp-agent
```

Replace the path with the actual path to your project folder.

You should see output like:
```
INFO: Started server process
INFO: Uvicorn running on http://0.0.0.0:8000
```

---

## Step 7 — Open the chat interface

Open your web browser and go to:

```
http://localhost:8000
```

You should see the ADK chat interface. Select **sre_agent** from the dropdown if it's not already selected.

---

## Step 8 — Start investigating

Type a message like:

```
Check what's broken on instance i-0abc123def456 and give me an RCA
```

The agent will ask you for the path to your docker-compose.yml file on the EC2 instance (for example `/home/ubuntu/myapp/docker-compose.yml`), then start investigating automatically.

---

## Where to find RCA reports

After every investigation, the agent saves a report to:

```
sre_agent/rca_reports/
```

Each file is named with the alert name, instance ID, and timestamp.

---

## Troubleshooting

**"No agents found"**
Make sure you're running `adk web` from the parent directory that *contains* the `sre_agent` folder, not from inside it.

**"SSM command timed out"**
Your EC2 instance may not have SSM Agent running, or the IAM role is missing. See Step 5.

**"GEMINI_API_KEY is not set"**
Check that `sre_agent/.env` has your key filled in and that you restarted `adk web` after editing it.

**"410 Gone" from Alertmanager**
Your Alertmanager version uses the v2 API. The agent already handles this. If you still see it, start a new session in the chat UI.

**Agent asks for container name instead of finding it**
Start a new session — old sessions can carry stale context.
