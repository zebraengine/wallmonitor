# Actionable notifications (local-only)

*[← back to the README](../README.md)*

Events a user can actually act on
are pushed, not just logged: a **predicted derate** while there is still
time to intercede (with the computed highest charge current that avoids
the trip — capping the vehicle beats the charger's blunt 50% foldback),
device alerts as they raise, the degradation watch's inspect-wiring
warning, and charger-unreachable. Two delivery paths, both consistent
with the nothing-phones-home rule: **browser notifications** from any
open dashboard tab (fed by the existing SSE stream — no push service,
no external traffic; enable via the bell in the header — browsers only
permit notifications on HTTPS or localhost, so over plain http:// on a
LAN IP the bell explains itself and you need an SSH tunnel to localhost
or an HTTPS front-end), and an optional
**LAN webhook** (`--notify-url` / `WM_NOTIFY_URL`) that POSTs each
warning as JSON to an endpoint on your own network — Home Assistant, a
self-hosted ntfy, Node-RED — for warnings while no dashboard is open.

## Phone alerts via self-hosted ntfy

The actionable warnings (predicted derate with a suggested current cap,
device alerts, drift, charger unreachable) can reach a phone through a
[ntfy](https://ntfy.sh) server you run yourself — message content never
leaves your LAN. `deploy/ntfy/docker-compose.yml` runs it next to the
monitor:

```bash
cd wallmonitor/deploy/ntfy
# edit NTFY_BASE_URL in docker-compose.yml to this box's LAN address first
docker compose up -d
cd ..
sudo ./install-service.sh --host <wall-connector-ip> [your other flags] \
  --notify-url http://127.0.0.1:8481/wallmonitor --notify-format ntfy
```

On the phone, install the ntfy app, point it at `http://<box-lan-ip>:8481`
as the default server, and subscribe to the `wallmonitor` topic. Warnings
arrive prioritized (a predicted derate is *urgent* — it's the one you can
act on in the moment by lowering the vehicle's charge current).

iOS caveat, stated plainly: Apple only delivers instant background pushes
through its own push service, so a purely self-hosted server means the iOS
app refreshes on open or periodically instead of instantly. Uncommenting
`NTFY_UPSTREAM_BASE_URL: https://ntfy.sh` in the compose file restores
instant delivery by sending **only a wake-up ping** ("check your server")
through ntfy.sh and Apple — the message content is still fetched from your
own box over the LAN. That's the closest iOS gets to local-only push;
Android needs no upstream at all. Off the home network, a VPN into your
LAN (e.g. WireGuard on the router) keeps everything reachable without
exposing anything.
