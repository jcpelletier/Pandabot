# Discord Bot Setup Guide

## 1. Create the Discord Application & Bot

1. Go to https://discord.com/developers/applications → **New Application** → name it "Panda"
2. Left sidebar → **Bot** → **Add Bot** → confirm
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**
4. Copy the **Token** → this goes into `.env` as `DISCORD_TOKEN`
5. Left sidebar → **OAuth2 → URL Generator**
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Message History`, `View Channels`
   - Copy the generated URL → open it → invite the bot to your server
6. In Discord: **Settings → Advanced → Developer Mode** (on)
7. Right-click your target channel → **Copy Channel ID** → paste into `.env` as `DISCORD_CHANNEL_ID`

---

## 2. Azure App Insights (optional — needed for `recent_rips`)

The `query_ripping: recent_rips` query and bot telemetry require an Azure App Registration with API access to your Application Insights resource.

1. In the Azure Portal → **Entra ID → App registrations → New registration** → name it (e.g. `pandabot`)
2. Copy **Application (client) ID** → `.env` as `AZURE_CLIENT_ID`
3. Copy **Directory (tenant) ID** → `.env` as `AZURE_TENANT_ID`
4. **Certificates & secrets → New client secret** → copy value → `.env` as `AZURE_CLIENT_SECRET`
5. Go to your **Application Insights resource → Access control (IAM) → Add role assignment**
   - Role: **Monitoring Reader**
   - Assign to the app registration from step 1
6. Copy the **Application ID** from App Insights → Overview → `.env` as `APPINSIGHTS_APP_ID`
7. Copy the **Instrumentation Key** → `.env` as `APPINSIGHTS_IKEY`
8. Set `APPINSIGHTS_ENDPOINT` to your regional ingestion endpoint (shown in App Insights → Overview → Connection String)

---

## 3. Get a Jenkins API Token

1. Browse to http://panda:8080
2. Top-right → your username → **Configure**
3. **API Token** section → **Add new Token** → generate → copy it
4. Paste into `.env` as `JENKINS_TOKEN`

---

## 4. Install on the Server

```bash
# SSH into the server and run the installer
ssh panda
curl -fsSL https://raw.githubusercontent.com/jcpelletier/Pandabot/main/install.sh | sudo bash
```

The installer will:
- Create a `discord-bot` system user (no login shell)
- Add it to the `docker` group (needed for `docker logs`)
- Create `/opt/discord-bot/` with a Python venv
- Install the systemd unit file

---

## 5. Configure .env

```bash
sudo nano /opt/discord-bot/.env
```

Fill in every value.  Generate a webhook secret with:

```bash
openssl rand -hex 24
```

---

## 6. Start the Bot

```bash
sudo systemctl enable --now discord-bot
sudo journalctl -fu discord-bot
```

You should see:

```
Webhook server listening on 127.0.0.1:8765/notify
Logged in as Panda#1234 (id=...)
```

---

## 7. Wire Jenkins Notifications

Add this as a post-build **Execute shell** step in each Jenkins job you want
notifications from.  The script reads `$BUILD_RESULT` which Jenkins sets
automatically after the build.

### For Login_Test (hourly — notify on all results so you see outages):

```groovy
// In Jenkins Pipeline (Jenkinsfile) — add a post block:
post {
    always {
        sh '''
          /opt/discord-bot/notify-discord.sh \
            "${JOB_NAME}" "${currentBuild.result ?: 'SUCCESS'}" \
            "${BUILD_NUMBER}" "${BUILD_URL}"
        '''
    }
}
```

### Or as a freestyle job "Post-build Action → Execute shell":

```bash
/opt/discord-bot/notify-discord.sh \
  "$JOB_NAME" "$BUILD_RESULT" "$BUILD_NUMBER" "$BUILD_URL"
```

> Tip: for Process_Movies and Nightly_Convert you probably only want failure
> alerts.  Wrap the call in `[ "$BUILD_RESULT" != "SUCCESS" ] && ...` or use
> the Jenkinsfile `post { failure { ... } }` block.

---

## 8. SMART Drive Health

`install.sh` installs `smartmontools` and grants `smartctl` the Linux capabilities
it needs to read drive data without `sudo` (compatible with the service's
`NoNewPrivileges=true` hardening):

```bash
sudo apt install smartmontools libcap2-bin
sudo setcap cap_sys_rawio,cap_dac_read_search+ep /usr/sbin/smartctl
```

No sudoers entry needed. Ask `@Panda check drive health` to use it.

---

## 9. Jenkins Timezone

Jenkins runs in a Docker container. Set its timezone so build log timestamps match the server:

```bash
# Add TZ to /opt/jenkins/docker-compose.yml under environment:
#   - TZ=America/New_York
sudo docker compose -f /opt/jenkins/docker-compose.yml up -d --force-recreate jenkins
```

---

## 9. Test it

**Test the webhook directly** (on the server):

```bash
source /opt/discord-bot/.env
curl -s -X POST http://127.0.0.1:8765/notify \
  -H "Content-Type: application/json" \
  -d "{\"secret\":\"$WEBHOOK_SECRET\",\"job_name\":\"Test\",\"status\":\"FAILURE\",\"build_number\":1,\"build_url\":\"http://example.com\",\"message\":\"webhook test\"}"
```

**Test the bot** — mention it in Discord:

```
@Panda how much disk space is left on the media drive?
@Panda what did the last Login_Test run do?
@Panda is Jellyfin running?
@Panda show me the last 20 lines of the rip-video log
```

---

## Conversation examples

| You ask | Claude calls |
|---|---|
| "how much space left on media drive?" | `get_disk_usage` |
| "did last night's convert job succeed?" | `get_jenkins_build_status(Nightly_Convert)` |
| "is sunshine running?" | `get_service_status(sunshine)` |
| "what's the GPU doing?" | `get_system_stats` |
| "why did the last rip fail?" | `get_log_tail(rip-video, 100)` |
| "give me a full health check" | all five tools |
