# LinkBound Phase 0 browser pilot

This pilot tests one headed Chrome profile on Linode. It performs no outbound
action. Hosted sends remain disabled until the account owner reviews the result.

## Host layout

- Ubuntu 24.04 on one Linode, with a non-root `linkbound` service account.
- TigerVNC `Xtigervnc` owns display `:1` and listens on loopback port 5901.
  Openbox is the window manager on that display.
- noVNC/websockify listens on loopback port 6080 and points at the same Xvnc
  display. Tailscale Serve exposes it inside the tailnet on HTTPS port 8443.
- The first Chrome profile lives under `/var/lib/linkbound/profiles/me`.
  Pilot evidence is under `/var/lib/linkbound/evidence`. Code is under
  `/opt/linkbound/current`; a code update never deletes the profile.
- `linkbound-display`, `linkbound-window-manager`, and `linkbound-novnc` start
  with the host. `linkbound-phase0-pilot` starts only when an operator requests
  a no-send browser check.

## Current host access

- The private viewer is at
  `https://linkbound-01.tailfbed29.ts.net:8443/vnc.html`. Its VNC password is
  stored only in `/var/lib/linkbound/.vnc/passwd`, not in the repository.
- SSH to the host's Tailscale address `100.103.144.62` using the provisioned
  key. The public IPv4 address is not an SSH access path.
- The tailnet policy grants the operator device access to TCP 8443 and 22 on
  this host. The host firewall denies other incoming connections, allows
  traffic on `tailscale0`, and allows UDP 41641 for Tailscale transport. The
  Linode LISH console is the recovery path if tailnet access fails.

## Verify the infrastructure

Run `systemctl is-active linkbound-display linkbound-window-manager linkbound-novnc`.
All three services must report `active`. Check `ss -ltnp` and confirm that 5901
and 6080 listen only on `127.0.0.1` or `::1`. The browser viewer must open through
Tailscale Serve, and neither raw VNC nor websockify may answer on the public IP.

## No-send browser check

1. Start `linkbound-phase0-pilot.service`. It uses the existing
   `LinkedInRunner.start()` and `open_feed()` methods, but never calls
   `process()` or a send method.
2. Open the noVNC HTTPS URL in an authorized tailnet browser. Confirm that the
   Chrome window launched by the service is visible and controllable. Complete
   LinkedIn login and 2FA manually if requested. Do not paste credentials into
   service logs or the repository.
3. Let the pilot close Chrome, then start it again with the same profile path.
   Confirm the login and feed navigation still work without another login.
4. Before opening any unread conversation, record its LinkedIn unread state.
   Check whether viewing it changes that state or creates a read receipt, and
   record the observation. This is a manual test, not an automatic inbox sync.
5. Review the JSON evidence from both runs and any login or challenge behavior.
   Record the observed Chrome and Playwright versions and the account owner's
   decision before enabling hosted sends.

If the VM's new IP or environment triggers a challenge, pause the pilot for
manual recovery. A successful pilot shows feasibility for this account at this
time; it is not a guarantee of future account behavior.

## Code updates

Build a Git archive from a reviewed commit and unpack it into a new
`/opt/linkbound/releases/<commit>` directory. Stop the pilot before switching
`/opt/linkbound/current` to that release, then restart it. Run
`systemctl daemon-reload` first if a unit changed. Profile, evidence, VNC
password, and application data remain under `/var/lib/linkbound`, outside the
release. To roll back, stop the pilot, point `current` at the prior release,
and start it again. Check `journalctl -u linkbound-phase0-pilot.service` and the
latest evidence JSON after either change.
