# LinkBound access options

Status: access decision made 2026-09-23. Use Tailscale Serve for the LinkBound UI and TigerVNC Xvnc plus noVNC for the server browser view. Other options below remain comparison material.

## The actual use case

- One human owner uses an always-on LinkBound CRM, campaign queue, exports, and API. He switches among several LinkedIn sender accounts; these are browser sessions, not LinkBound users.
- A headed Chrome process on Linode performs scheduled work with a separate persistent profile per LinkedIn account. The owner sometimes needs to see and control that same Chrome window for login, challenges, and diagnosis.
- The app should be easy to open from the owner's devices. The browser desktop is an administrator surface and can require a less convenient access path if interventions are rare.
- No human input may race an automated job or open a second Chrome process against the same profile. Playwright documents that one user-data directory cannot be used by multiple browser instances at once.
- The later Cruitical integration needs its own service credential. If LinkBound pushes to Cruitical, Cruitical does not need to reach the LinkBound UI; if Cruitical calls LinkBound, the ingress must admit a machine identity.
- Access to LinkBound and the remote desktop is inbound traffic to Linode. Chrome's outbound connection to LinkedIn is independent of these choices.

## 1. How the owner reaches the LinkBound web app

| Option | What the owner does | What it buys us | Cost or limit |
| --- | --- | --- | --- |
| SSH local port forward | Start an SSH tunnel, then open `localhost` in a browser | Fewest moving parts for a pilot; app remains bound to localhost | Tunnel must be started and kept open on each device; poor everyday experience |
| Tailscale Serve | Enroll the Linode and each owner device, then open a private HTTPS URL | No public app endpoint or custom domain; device access rules | Requires a Tailscale client and account on each device; another control plane |
| Self-managed WireGuard | Install VPN clients and connect to the VM's private VPN address | Private route without a managed mesh provider | Peer keys, routing, revocation, and the VPN endpoint are ours to operate; does not itself supply app authentication or HTTPS |
| Identity-aware web proxy, such as Cloudflare Access plus Tunnel | Open a normal HTTPS URL and complete a browser login | Browser-only access, identity policy before requests reach the app, no inbound application port on the VM when using Tunnel | Requires a domain, DNS/provider setup, identity policy, and a running tunnel; the provider is in the access path |
| Public HTTPS reverse proxy plus LinkBound login | Open a normal HTTPS URL and log in to LinkBound | Direct control with no VPN or external access proxy | Publicly reachable login and API surface; we own MFA, rate limits, secure sessions, patching, and incident response |

No ingress option removes LinkBound's need to authorize its own run controls, files, exports, WebSockets, and service API. LinkBound can use the verified Tailscale Serve identity for its one human owner, while service calls need a separate credential.

## 2. How the owner sees the actual server Chrome window

| Option | How it works | Fit |
| --- | --- | --- |
| Linode Glish | Cloud Manager shows the VM's VGA desktop in a browser | Emergency console if the normal display or remote view fails. It has no clipboard and may not show the Xvnc display used by automation. |
| TigerVNC Xvnc plus noVNC | Xvnc provides a virtual X display and VNC server; noVNC renders it in a normal browser | Selected. The browser worker opens Chrome on this display; noVNC shows the same display through a separate Tailscale Serve endpoint. |
| Xvfb plus a VNC sharing server | Xvfb provides the display; a separate process shares it | Established headed-browser pattern, but more components than Xvnc when interactive viewing is required. |
| Native VNC client over SSH or VPN | A desktop VNC application connects to the same display | Simple transport when a native client is acceptable; less convenient than noVNC. |
| RDP desktop, optionally through Apache Guacamole | A Linux desktop is served over RDP; Guacamole makes it browser-accessible | Useful for general remote workstation access or many desktops. It may open a different session from the automation browser, and adds services to manage. |
| Xpra or Chrome Remote Desktop | Application or desktop streaming through another remote-access stack | Valid alternatives, but add another session model or external account. They need the same proof that manual access sees the automation-owned Chrome profile. |

The display choice and the access path are independent. For example, Xvnc plus noVNC can be viewed through an SSH tunnel, a mesh VPN, or an identity-aware HTTPS proxy. A screenshot or Playwright trace helps diagnose a job but is not a substitute for interactive login or challenge handling. Raw VNC and Chrome DevTools ports should not be public.

## Selected setup

1. **Pilot and production use the same access pattern:** Enroll Linode and the owner's devices in the tailnet. Run Tailscale Serve in persistent background mode for two private HTTPS endpoints, for example the LinkBound app on 443 and the noVNC viewer on 8443. Apply tailnet grants to the owner and approved devices. Do not enable Funnel for either endpoint.
2. **The browser display is Xvnc:** Run Xvnc under a non-root service account. Point the Playwright worker's `DISPLAY` at it and run noVNC with websockify against its local VNC socket. Bind the app, websockify, and raw VNC listeners to loopback; expose no public app, VNC, or Chrome debugging port. Use a VNC password as a second check for desktop control.
3. **Manual control is coordinated:** The viewer observes the actual worker display. Before taking control of an account profile, pause that account's scheduled work, wait for the active browser operation to finish, and hold its profile lock. Do not launch a second Chrome process against the same profile. Glish remains a VM recovery console, not the normal LinkedIn browser viewer.

Tailscale simplifies private access, but the app still has to authorize sensitive UI actions and API calls. For the one human owner, an allowlist of Tailscale Serve identity headers can avoid a second login while the backend listens only on loopback. Service-to-service calls use a separate scoped credential; tagged devices do not receive Tailscale user identity headers. These rules apply to HTTP, WebSocket, exports, file downloads, and run controls.

The hosted browser pilot decides whether Linode is a suitable LinkedIn worker. None of these display or ingress methods guarantees that LinkedIn will treat the new VM, IP, or browser environment like the current local setup. If the hosted pilot fails, an always-on local worker is a separate fallback; it is not solved by changing the remote-access method.

## Primary references

- [Linode Glish](https://techdocs.akamai.com/cloud-computing/docs/access-your-desktop-environment-using-glish)
- [Playwright persistent contexts and profile exclusivity](https://playwright.dev/python/docs/api/class-browsertype)
- [TigerVNC](https://github.com/TigerVNC/tigervnc) and [noVNC](https://github.com/novnc/noVNC)
- [OpenSSH local forwarding](https://man.openbsd.org/ssh.1)
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) and [WireGuard](https://www.wireguard.com/)
- [Cloudflare Access application types](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/choose-application-type/), [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/get-started/), and [service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)
- [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https) and [Apache Guacamole](https://guacamole.apache.org/doc/gug/guacamole-architecture.html)
