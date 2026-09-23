# LinkBound access options

Status: researched options for the Linode pilot and production access. No access provider has been selected for production.

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

No ingress option removes LinkBound's need to authorize its own run controls, files, exports, WebSockets, and service API. An identity-aware proxy is an outer gate; LinkBound still needs an account-aware application session and service credentials.

## 2. How the owner sees the actual server Chrome window

| Option | How it works | Fit |
| --- | --- | --- |
| Linode Glish | Cloud Manager shows the VM's VGA desktop in a browser | First thing to try in the pilot. It adds no VNC service, but needs a lightweight desktop and display manager, and Glish has no clipboard. We must prove the automation launches Chrome on that same display. |
| TigerVNC Xvnc plus noVNC | Xvnc provides a virtual X display and VNC server; noVNC renders it in a normal browser | Best fallback when Glish is awkward or when a direct browser-view URL and clipboard are needed. Use SSH forwarding in the pilot; protect any production endpoint separately. |
| Xvfb plus a VNC sharing server | Xvfb provides the display; a separate process shares it | Established headed-browser pattern, but more components than Xvnc when interactive viewing is required. |
| Native VNC client over SSH or VPN | A desktop VNC application connects to the same display | Simple transport when a native client is acceptable; less convenient than noVNC. |
| RDP desktop, optionally through Apache Guacamole | A Linux desktop is served over RDP; Guacamole makes it browser-accessible | Useful for general remote workstation access or many desktops. It may open a different session from the automation browser, and adds services to manage. |
| Xpra or Chrome Remote Desktop | Application or desktop streaming through another remote-access stack | Valid alternatives, but add another session model or external account. They need the same proof that manual access sees the automation-owned Chrome profile. |

The display choice and the access path are independent. For example, Xvnc plus noVNC can be viewed through an SSH tunnel, a mesh VPN, or an identity-aware HTTPS proxy. A screenshot or Playwright trace helps diagnose a job but is not a substitute for interactive login or challenge handling. Raw VNC and Chrome DevTools ports should not be public.

## Recommended decision sequence

1. **Phase 0:** Try Linode Glish with a lightweight desktop and one persistent Chrome profile. Verify a non-root Playwright worker launches a headed Chrome window visible in Glish, that manual login survives restart, and that the owner can recover a challenge. Glish's lack of clipboard is an explicit usability check. If it fails or is too awkward, use Xvnc plus noVNC bound to localhost and reach it through an SSH tunnel. Neither path requires Tailscale.
2. **Production CRM access:** If opening LinkBound from an ordinary browser without installed client software is important, favor an identity-aware HTTPS proxy. Cloudflare Access plus Tunnel is one concrete implementation, subject to the owner's domain and provider preference. If access should be restricted to enrolled devices instead, Tailscale Serve is simpler than operating WireGuard directly. A public reverse proxy plus app login is viable only after LinkBound's authentication work is complete and reviewed.
3. **Production browser access:** Keep Glish if it proves comfortable for occasional interventions. Add noVNC behind a separately protected URL only if frequent manual access or a direct LinkBound-to-browser link justifies it. Manual control must pause scheduled work and hold the account profile lock.

The hosted browser pilot decides whether Linode is a suitable LinkedIn worker. None of these display or ingress methods guarantees that LinkedIn will treat the new VM, IP, or browser environment like the current local setup. If the hosted pilot fails, an always-on local worker is a separate fallback; it is not solved by changing the remote-access method.

## Primary references

- [Linode Glish](https://techdocs.akamai.com/cloud-computing/docs/access-your-desktop-environment-using-glish)
- [Playwright persistent contexts and profile exclusivity](https://playwright.dev/python/docs/api/class-browsertype)
- [TigerVNC](https://github.com/TigerVNC/tigervnc) and [noVNC](https://github.com/novnc/noVNC)
- [OpenSSH local forwarding](https://man.openbsd.org/ssh.1)
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) and [WireGuard](https://www.wireguard.com/)
- [Cloudflare Access application types](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/choose-application-type/), [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/get-started/), and [service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)
- [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https) and [Apache Guacamole](https://guacamole.apache.org/doc/gug/guacamole-architecture.html)
