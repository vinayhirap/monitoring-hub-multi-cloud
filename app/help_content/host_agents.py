# app/help_content/host_agents.py
"""Help Center section for host agents (the small program installed on a server that pushes its own metrics).

Written for end users, in plain language. Every number below is read from the code that enforces it, so the page
cannot drift from the product:
  * stale / offline windows       app/hostagents/settings.py (stale_seconds) and app/collector/host_agent_watchdog.py
  * report interval limits        app/hostagents/settings.py (INTERVAL_MIN_SECONDS, INTERVAL_MAX_SECONDS, DEFAULT_CONFIG)
  * service / path / file names   agent/install.sh (the installer is the source of truth)
This is NOT the AI ops agent (app/agent, /api/agent).
"""
from app.hostagents import settings as ha

from .blocks import h, p, ul, table, note, code, link, section, fmt_interval

MON = "Monitoring"
# Same value as app/collector/host_agent_watchdog.OFFLINE_FACTOR (not imported: that module pulls in the database layer,
# and help content must stay pure). tests/test_help_content.py asserts the two agree.
OFFLINE_FACTOR = 5


def host_agents():
    interval = ha.DEFAULT_CONFIG["interval_seconds"]
    stale = ha.stale_seconds()
    stale_txt = fmt_interval(stale)
    offline_txt = fmt_interval(stale * OFFLINE_FACTOR)
    return section("host-agents", "Host agents (install on a server)", MON, "viewer", "hostagents.view", [
        p("A host agent is a tiny program you install on a server. Every minute it looks at the server from the inside "
          "(how busy the processor is, how full the memory and disks are, how much network traffic there is) and sends "
          "those numbers to this hub. The hub shows the server on the Host Agents page and raises an alert if the server "
          "goes quiet."),
        note("Normal cloud monitoring asks AWS about your server from the outside. A host agent reports from inside the "
             "server, so it sees things the cloud cannot, such as real memory and disk use. It also works if the cloud "
             "API is slow, and it makes no cloud API calls, so it adds no cloud monitoring charges."),
        link("Open Host Agents", "/host-agents"),

        h("How it works, in four steps"),
        ul([
            "An administrator clicks Add host. The hub creates a one-time enrollment key (a long code starting with cak_). "
            "The key is shown once, then only its fingerprint is stored.",
            "On the server, someone runs the install command shown next to the key. The installer downloads the agent, "
            "checks the file has not been altered, creates a locked-down service account and registers the server using "
            "the key. The hub swaps the key for a private token that only that one server holds.",
            f"From then on the agent sends a report about every {fmt_interval(interval)}. The server never opens a port "
            "or accepts a connection: it only calls out to the hub over HTTPS. If the hub cannot be reached, readings are "
            "kept on the server's disk for a while and sent when the connection returns.",
            "The hub stores the readings next to your other metrics and shows the server on the Host Agents page. "
            "If reports stop, the hub warns you (see Alerts below).",
        ], ordered=True),

        h("What it reports"),
        p("Linux reports everything in this table. macOS and Windows report what their operating system can give an "
          "honest number for; anything not available is left out, never shown as zero."),
        table(["Reading", "Linux", "macOS", "Windows"], [
            ["Processor busy %", "yes", "yes", "yes"],
            ["Load average (1, 5, 15 min)", "yes", "yes", "-"],
            ["Memory used % and memory available", "yes", "yes", "yes"],
            ["Swap used %", "yes", "yes", "-"],
            ["Disk used % (each disk)", "yes", "yes", "yes"],
            ["Disk files-used % (inodes)", "yes", "-", "-"],
            ["Disk read / write speed", "yes", "-", "-"],
            ["Network in / out speed (each interface)", "yes", "yes", "-"],
            ["Number of processes", "yes", "yes", "yes"],
            ["Time since last restart", "yes", "yes", "yes"],
        ]),
        p("Memory and disk use are stored under the same names as the existing CloudWatch agent readings, so the "
          "thresholds and charts you already use for those apply to a host agent too. The other readings are stored "
          "under their own names starting with host_ so they are never mixed with a cloud-sourced series."),

        h("The four states"),
        table(["State", "What it means", "What you will see"], [
            ["Online", f"The hub heard from the server within the last {stale_txt}.", "Nothing: all is well."],
            ["Stale", f"Silent for more than {stale_txt} but less than {offline_txt}.", "A warning alert."],
            ["Offline", f"Silent for {offline_txt} or more.", "The same alert, raised to critical."],
            ["Revoked", "An administrator cut this server off.", "No alerts. The server stays in the list as a record."],
        ]),
        p(f"One missed report never raises an alert: nothing happens until the server has been silent for {stale_txt}. "
          "An administrator can change that window with HOST_AGENT_STALE_SECONDS in the server .env; "
          f"the offline limit is always {OFFLINE_FACTOR} times the stale window."),

        h("Adding a server (administrators)"),
        ul([
            "Open Infrastructure, then Host Agents, then click Add host.",
            "Choose the account the server belongs to. Optionally add a label such as web fleet.",
            "Max uses is how many servers may register with this key (1 to 1000). Expires after is how long the key stays "
            "valid, in hours (1 to 8760). Use the smallest numbers that fit: one key per batch of servers.",
            "Click Create key. Copy the key and the install command now, because the key cannot be shown again. "
            "The window cannot be closed by accident until you confirm you saved it.",
            "Run the install command on the server. Linux with systemd is what the command is written for; it needs a "
            "user who can use sudo.",
            "Wait a minute or two and press Refresh. The server appears as Online. The page also refreshes by itself "
            "every 30 seconds.",
        ], ordered=True),
        note("The install command downloads the agent from this hub. An administrator must have published an agent release "
             "on the hub first; if the download fails with 404, ask your administrator. If the hub is plain http "
             "(test setups only), the command adds --insecure-dev. Never use that on a real network.", tone="warn"),

        h("macOS and Windows"),
        p("Use the installer for your system from the same release folder. Replace the hub name and key; the folder is "
          "the version number your administrator published."),
        code("# macOS (Terminal)\n"
             "sudo ./install-macos.sh --base-url https://<hub>/dl/host-agent/<version> --url https://<hub> --key cak_...\n"
             "\n"
             "# Windows (PowerShell opened as Administrator)\n"
             ".\\install.ps1 -BaseUrl https://<hub>/dl/host-agent/<version> -Url https://<hub> -Key cak_..."),

        h("Looking at your servers"),
        ul([
            "The state buttons at the top (All, Online, Stale, Offline, Revoked) filter the list and show how many are in each state.",
            "Search matches the host name, instance id and operating system. The account menu limits the list to one account.",
            "You only see servers in accounts you have access to.",
            "Click a server to open its panel: account, instance id, operating system, agent version, when it last "
            "reported and any firing alerts that name this server.",
        ]),

        h("Changing or removing a server (administrators)"),
        ul([
            "Configure (in the server's panel) changes how often it reports (15 to 300 seconds) and which services to "
            "watch. Shorter intervals give finer charts but store more data. It takes effect on the server's next report.",
            "Revoke host cuts the server off at once: its next report is refused and it shows as Revoked. This does not "
            "uninstall anything on the server itself.",
            "Enrollment keys (button at the top) lists every key with its use count, expiry and status "
            "(active, exhausted, expired or revoked). Revoke a key you no longer need, because a valid key can still "
            "register a server, even one that was revoked before.",
        ]),
        p("To fully remove an agent from a Linux server, revoke it in the hub first, then run this on the server:"),
        code("sudo systemctl disable --now cloudops-agent\n"
             "sudo rm -f /etc/systemd/system/cloudops-agent.service /usr/local/bin/cloudops-agent\n"
             "sudo systemctl daemon-reload\n"
             "# optional: also delete its saved token, queue and service account\n"
             "sudo rm -rf /etc/cloudops-agent /var/lib/cloudops-agent && sudo userdel cloudops-agent"),

        h("Alerts"),
        ul([
            "Silent server: the warning and critical alerts described under The four states. There is one alert per "
            "server, not a new one every few minutes.",
            "Agent down or server down? If the cloud says the instance is stopped, the hub marks the agent offline "
            "quietly and does not alert. If the instance is running but the agent is silent, the alert says the agent or "
            "its network path is down. If the cloud's own health checks are also failing, it says the server itself may "
            "be impaired. For servers the hub cannot match to a cloud instance, it says the two cannot be told apart.",
            "Maintenance windows silence these alerts like any other.",
        ]),
        p("Busy processor, full disk and similar alerts use normal thresholds. For host agent readings the default "
          "thresholds ship switched off, so an administrator has to turn them on for an account before they can alert.",
          unverified=True),
        link("Alerts", "#alerts"),
        link("Thresholds", "#thresholds"),
        link("Maintenance windows", "#maintenance-windows"),

        h("Is it safe?"),
        ul([
            "The agent opens no port. It only calls the hub, over HTTPS (it refuses plain http unless started with --insecure-dev).",
            "It runs as its own account with no login and no extra privileges, with memory and processor caps, not as root.",
            "The installer refuses to install a file whose checksum does not match the published one.",
            "The key is stored only as a fingerprint; the server's token is bound to that one server. Every key, revoke "
            "and change is written to the audit trail.",
            "It reads counters only. It does not read your files or application data.",
        ]),

        h("When something is wrong"),
        table(["What you see", "Likely cause", "What to do"], [
            ["The server never appears", "The install failed, the key was wrong or used up, or the feature is switched off on the hub.",
             "Check the installer output on the server. In Enrollment keys, check the key is active. Ask an administrator to confirm HOST_AGENT_ENABLED is true."],
            ["The installer says CHECKSUM MISMATCH", "The download was damaged or altered.", "Nothing was installed. Do not continue: tell your administrator."],
            ["The installer says it cannot download", "Wrong release folder, no release published, or the server cannot reach the hub on port 443.",
             "From the server run: curl -sS -o /dev/null -w '%{http_code}' https://<hub>/api/health/live (expect 200)."],
            ["Stale or Offline", "The server is off, the agent stopped, or the network path is blocked.",
             "On Linux: systemctl status cloudops-agent and journalctl -u cloudops-agent -n 50."],
            ["Revoked, but you want it back", "An administrator revoked it.", "Enroll it again with a new key; it becomes active with a fresh token."],
            ["Online, but some readings are dropped", "The server's clock is wrong: readings dated more than 10 minutes ahead or a day behind are discarded.",
             "Fix time sync on the server (timedatectl, or chronyc tracking)."],
            ["The agent stops and stays stopped", "The hub refused its token (host revoked).", "This is deliberate so it does not keep retrying. Re-enroll or uninstall it."],
        ]),
        note("Readings for a server that was offline are saved on its disk (about 20 MB, oldest dropped first) and sent when it "
             "reconnects. Readings older than about 23 hours are not replayed."),
    ], "Install a small agent on a server so it reports its own CPU, memory, disk and network")
