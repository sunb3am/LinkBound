# LinkBound Phase 0 pilot result

Observed on 2026-09-23 UTC. The host is `linkbound-01` on Ubuntu 24.04.4 LTS,
running Google Chrome 154.0.8037.57 with Playwright 1.63.0.

## Checks completed

- The existing `LinkedInRunner` launched headed Chrome on the server's Xvnc
  display, using `/var/lib/linkbound/profiles/me` as its persistent profile.
- The private noVNC URL returned HTTP 200 from the operator computer after the
  Tailscale grant was saved. SSH to the host's Tailscale IP succeeded.
- After enabling the host firewall, the private viewer and SSH still worked.
  Public SSH timed out. VNC port 5901 and websockify port 6080 listened only on
  loopback.
- The operator logged into LinkedIn in the viewer, then visually confirmed a
  signed-in feed after Chrome was closed and reopened with the same profile.
- The final no-send run wrote
  `/var/lib/linkbound/evidence/phase0-20260923T122258Z.json` with
  `initial_login_check: true`, `final_login_check: true`, and
  `outcome: completed`. Its only LinkedIn navigation was to the feed.
- Service stop and restart completed successfully. The pilot browser was left
  closed; display and viewer services remain active.

## Still to test

- Whether opening an unread LinkedIn conversation changes its unread state or
  produces a read receipt. No conversation was opened during this pilot.
- Whether a hosted outbound campaign triggers a LinkedIn challenge or notice.
  Hosted sends were not enabled in Phase 0.
