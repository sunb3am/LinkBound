# LinkedIn access and browser environment risk review

Research date: 2026-09-23. Scope: public LinkedIn documentation, primary
browser/Playwright documentation, the LinkBound code, and read-only inspection
of the Phase 0 Linode. This is an account-risk assessment, not evidence that a
browser is undetectable or that automated use is permitted. LinkedIn operations
remain browser-only; this review does not propose a LinkedIn API integration.

## What LinkedIn actually discloses

| Documented fact | Consequence for LinkBound | Source |
| --- | --- | --- |
| LinkedIn receives IP address, proxy, operating system, browser and add-ons, device identifiers and features, cookie IDs, and ISP information. It logs visits and actions such as messages and invitations. | A remote browser has a distinct network and device context even when it displays the same account. Tailscale protects access to our VM but does not make LinkedIn see a laptop's network. | [Privacy Policy](https://www.linkedin.com/legal/privacy-policy) |
| LinkedIn identifies browsers for abuse detection, uses cookies for bot detection and malicious-activity tracking, and records prior two-factor verification and CAPTCHA challenges. | Keep each owner's genuine browser profile and cookies intact. Do not copy, forge, rotate, or share profiles between accounts. | [Cookie Table, Security](https://www.linkedin.com/legal/l/cookie-table) |
| An unfamiliar device or location and suspicious web activity can trigger app approval, email verification, CAPTCHA, or identity checks. LinkedIn advises enabling cookies and avoiding VPNs or proxies to reduce sign-in challenges. | The first Linode sign-in may be treated as unfamiliar; LinkedIn does not say how it classifies this VM. The owner must handle any challenge manually. Repeatedly changing egress networks is contrary to LinkedIn's own advice. | [Security verification when signing in](https://www.linkedin.com/help/linkedin/answer/a1339220), [new-location notice](https://www.linkedin.com/help/linkedin/answer/a1337273) |
| High volumes of messages or other content in a short period can lead to reduced visibility or account restrictions. All accounts have invitation limits, but LinkedIn does not publish a generally safe count; an invitation restriction typically lasts a week. | The configured 100 invitations per day is our ceiling, not a LinkedIn allowance. A fixed interval or lower count cannot guarantee safety. | [High volume of content shared](https://www.linkedin.com/help/linkedin/answer/a1339697/high-volume-of-messages-sent?lang=en), [invitation limits](https://www.linkedin.com/help/linkedin/answer/a550555) |
| Unusually many page or profile views can trigger warnings or temporary viewing restrictions, and LinkedIn calls out systematic viewing as prohibited. | A large inbox/contact backfill has its own account risk even if it sends nothing. A daily collector should stop on these notices and report incomplete coverage. | [Restricted Action Message](https://www.linkedin.com/help/linkedin/answer/a1339210/restricted-action-message?lang=en), [Profile Scraping Limit Notification](https://www.linkedin.com/help/linkedin/answer/a1393432) |
| Third-party software that automates LinkedIn website activity is disallowed. Automated inauthentic activity can lead to temporary or permanent account restriction. | A headed Chrome window, persistent cookies, careful pacing, or a clean pilot do not change LinkedIn's stated rule. The operator authorized internal use of Shubham's account; engineering still has to verify its stop controls before enabling hosted sends. | [Automated activity](https://www.linkedin.com/help/linkedin/answer/a1340567/automated-activity-on-linkedin?lang=en), [Account restrictions](https://www.linkedin.com/help/linkedin/answer/a1340522), [User Agreement §8.2](https://www.linkedin.com/legal/user-agreement) |

The published material does **not** specify LinkedIn's detection model, signal
weights, thresholds, how it classifies cloud IPs, or a browser configuration
that it considers safe. A challenge or restriction can have several causes. We
cannot attribute a specific account event to one fingerprint signal without
evidence from LinkedIn. The public [Cookie Table](https://www.linkedin.com/legal/l/cookie-table)
does establish that browser identification and bot detection are part of its
security system, but it does not reveal how those decisions are made.

## General browser signals, with a separate evidence boundary

Websites can observe browser and platform information through HTTP headers,
[User-Agent Client Hints](https://developer.chrome.com/docs/privacy-security/user-agent-client-hints),
and JavaScript. The standardized [`navigator.webdriver`](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/webdriver)
property can indicate browser automation under documented launch conditions.
[Playwright](https://playwright.dev/python/docs/api/class-browsertype) exposes
headed/headless mode, persistent profile directories, timezone, user agent,
viewport, and Chrome channel options. These are general technical capabilities.
LinkedIn has not publicly confirmed which of these individual properties it
uses for account enforcement, so they are not presented here as a detection
recipe or as a checklist to spoof. We found no LinkedIn source establishing
that canvas, WebGL, font, TLS, or specific automation-property tests drive its
account restrictions.

## LinkBound versus the existing laptop workflow

| Area | Observed LinkBound state | Assessment and action |
| --- | --- | --- |
| Network | The Linode's observed outbound IPv4 is `50.116.8.251`. Tailscale Serve is private ingress for the current viewer and planned app UI. | The hosted Chrome uses the Linode network, not the operator's laptop network. LinkedIn may treat it as an unfamiliar location; keep the network stable and pause on a challenge. Do not rotate proxies or IPs in response. |
| Browser and OS | Ubuntu 24.04.4, Google Chrome 154.0.8037.57, Playwright 1.63.0, headed Chrome on a 1440x900 Xvnc display. The VM timezone is UTC and locale is `en_US.UTF-8`. | These are real, observable properties of a Linux VM and may differ from the laptop. Headed mode enables manual intervention; it is not proof of non-detection. Record actual values and avoid fabricated OS, user-agent, timezone, or device claims. |
| Browser launch | `app/runner.py` uses a Playwright persistent Chrome context and passes a nondefault `AutomationControlled` launch flag. The pilot uses the same runner. | The flag's presence does not establish that LinkedIn trusts this browser. Do not add stealth plugins or further overrides based on unverified claims. Review any browser-flag change in a separate no-send regression test; the owner reports that the local send path has worked reliably. |
| Session | `/var/lib/linkbound/profiles/me` is private to the non-root service user. The Phase 0 run reopened it and the owner confirmed a signed-in feed. | This proves session persistence for that test, not that LinkedIn has approved the environment. Keep one profile per account, one Chrome owner at a time, and protect profile backups as credentials. |
| Outbound behavior | `config.yaml` allows `daily_cap: 100`, a fixed 30-second gap, and work outside business-hour gating. The hosted environment overrides that with pilot ceilings of 5 actions per day and 20 per week; live sends are disabled. | These are internal limits with no published safe basis. Test the challenge, limit, and uncertain-send stops in a controlled hosted regression before enabling sends. Review each initial send result. Preserve the existing send selectors until a failing case is captured. |
| Inbound behavior | A bounded headed pilot proved unread marker restoration. The Chrome native attachment download crashed after restart; a browser-response capture now works in the hosted Shubham profile. Runs 4 and 5 saved one file and restored three unread markers. Run 6 showed a large inbox backlog. Run 7 verified the 20-row limit per folder and restored one unread marker. Run 8 stopped on the first imported tracked-contact identity check; no connection was inferred. No read-receipt reversal was established. | Keep pre-open unread intents, bounded coverage, and fail-closed recovery. The daily no-send job is paused until the tracked-profile stop is diagnosed. Stop on challenges and restricted-action notices. |
| Host security | noVNC and VNC bind to loopback and are reached through Tailscale. Chrome sandboxing passed a disposable headed test and is enabled for the hosted service. | Private ingress and browser sandboxing protect the host; neither establishes LinkedIn account-policy standing. Keep the browser under the non-root service account. |

## Risk controls and remaining checks

### Optional Tailscale exit node for hosted egress

On 2026-09-23, the Linode's Tailscale status showed no approved exit node and
the Linode was using its own internet route. Tailscale can route the Linode's
non-tailnet internet traffic through an approved laptop or phone, so LinkedIn
would see that exit device's upstream public IP. This addresses the cloud IP
part of the environment only. Chrome remains a Linux browser running on the
Linode, with the same profile and other browser signals. A phone used for
LinkedIn does not make the server browser appear to be that phone.

A plugged-in laptop on a stable home network is the first candidate for a
controlled trial. Tailscale documents iOS and Android phones as eligible exit
nodes too, but specifically warns that Android exit nodes are still being
optimized and may be too slow for most uses. A phone on the same Wi-Fi as the
laptop generally has the same home public IP; cellular egress can change as the
phone moves or the carrier changes its address. A selected exit node routes
the Linode's other internet traffic as well, including package downloads and
offsite backups, while tailnet traffic remains on the tailnet. Loss or key
expiry of an exit node can make internet routes unreachable, so the scheduler
must stay paused if the exit path is unhealthy rather than silently switching
egress during a LinkedIn run.

The trial gate is: approve and select one exit node; verify the new public
egress IP and DNS with a benign site; verify tailnet SSH, app, viewer, backup,
and restart behavior; then inspect the existing signed-in LinkedIn browser
headfully for any verification challenge. Record the change. Do not resume
scheduled scans or sends until the current tracked-profile stop is diagnosed
and a bounded no-send scan passes. LinkedIn itself recommends avoiding VPNs or
proxies to reduce sign-in challenges; there is no published assurance that a
Tailscale exit node lowers account risk.

An exit node needs separate approval in the Tailscale admin console. A
restrictive custom policy must grant the Linode `autogroup:internet`; a grant
to the exit device's tailnet IP alone does not authorize internet routing.
Tailscale's default allow-all policy permits approved exit node use.

Sources: [Tailscale exit nodes](https://tailscale.com/docs/features/exit-nodes),
[iOS setup](https://tailscale.com/docs/features/exit-nodes?tab=ios),
[router implementation](https://tailscale.com/docs/reference/kernel-vs-userspace-routers),
[LinkedIn security verification](https://www.linkedin.com/help/linkedin/answer/a1339220).

1. **Project decision:** On 2026-09-23, the operator authorized internal hosted
   use for the initial Shubham account and declined a separate account-owner
   approval workflow. A successful no-send login remains a feasibility check.
   Keep hosted sends disabled until the stop controls and a bounded headed
   regression are demonstrated; authorization does not change LinkedIn's rule.
2. **Environment baseline:** Record the actual browser version, OS, timezone,
   locale, display, outbound IP, account/profile ownership, and observed login
   challenges in a no-send run. Compare changes over time. Do not synthesize a
   different identity or transplant laptop cookies to the VM.
3. **Stop conditions:** Centralize detection of login/checkpoint/CAPTCHA pages,
   invitation or profile-view limits, restricted-action notices, and session
   loss. Pause that account and require a person to inspect it. Never auto-retry
   an invitation or message after an uncertain click.
4. **Controlled activity pilot:** Use a conservative per-account budget and
   review every initial send and account notice. Keep the current local browser
   workflow as the fallback. Do not treat absence of a warning as proof of safety.
5. **Inbound scan discipline:** The unread-marker effect has been tested; a
   restored marker may not undo a read receipt. The hosted attachment path now
   works without native Chrome downloads. Keep the daily scan bounded, record
   partial coverage and failures, and observe the first automatic daily run.
   A challenge halts sync rather than prompting alternate routes
   or escalating request volume.

The environment baseline, browser host security, and central stop controls
are implemented. Their live hosted send behavior still needs a controlled
regression. Broader inbox coverage remains part of Phase D.
The review provides no basis to claim the VM is a laptop or that browser
automation is undetectable.

## Risk-control implementation

The branch adds a repeatable, non-secret Linux environment snapshot, hosted
pilot ceilings of 5 actions per day and 20 queued actions per week, visible
page and HTTP stop checks before browser actions, and campaign pauses after a
limit or uncertain result. It removes the message Enter retry after an
unconfirmed send and makes invitation send-button click errors stop rather
than trying another submit method. These changes are covered by local tests;
they do not show that every LinkedIn warning can be recognized.

The Chrome sandbox succeeded in a disposable headed Linode profile and is
enabled in the hosted service. The temporary directory workaround did not
survive stronger offline testing. Instead, the current collector captures a
visible attachment click's response bytes through Chrome DevTools and denies
the native download manager for that click. An offline slow 2 MiB PDF transfer
succeeded across repeated profile restarts. Hosted no-send runs 4 and 5 then
saved one real 52248-byte attachment without a browser crash and restored
three unread markers. The file API and ZIP export bytes matched the stored
SHA-256. The internal cause of Chrome's native-download `SIGSEGV` remains
unknown. Hosted live sending remains disabled. The 20-row daily path passed a
hosted no-send pilot and is enabled for Shubham. Its first automatic run still
needs to be observed.

## Observation record

The host observations above were checked by read-only SSH on 2026-09-23 UTC:
`timedatectl` returned `Timezone=Etc/UTC`; `locale` returned
`LANG=en_US.UTF-8`; `google-chrome --version` returned
`Google Chrome 154.0.8037.57`; `pip show playwright` returned `1.63.0`;
and an IP echo from the VM returned `50.116.8.251`. `stat` showed
`linkbound 700 /var/lib/linkbound/profiles/me`. `ss` showed ports 5901 and
6080 bound to loopback, and `ufw status` showed no public SSH allow rule.
The Phase 0 Chrome process inspection showed `--no-sandbox`; the current
browser runner does not request Playwright's `chromium_sandbox` option.
The final no-send evidence file on the VM,
`/var/lib/linkbound/evidence/phase0-20260923T122258Z.json`, recorded
`initial_login_check: true`, `final_login_check: true`, and
`outcome: completed`. The account owner separately confirmed the signed-in
feed after Chrome restarted. The [Phase 0 pilot report](https://github.com/sunb3am/LinkBound/blob/codex/linkbound-phase0/docs/linkbound-phase0-pilot-report.md)
records the same checks. None of these observations proves that LinkedIn
regards the environment as low risk.
