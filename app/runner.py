"""Playwright runner: processes one LinkedIn profile at a time.

The flow is now explicit and auditable:
  1. detect_page_state()  -> degree, Connect availability, Message button, pending
  2. decide_action()      -> the concrete action (or a terminal status)        [decision.py]
  3. do_<action>()        -> performs it, returning an outcome + decision trace

The single most important guarantee: we never silently escalate to InMail. If we
cannot Connect, we flag the profile rather than messaging a non-connection (which
LinkedIn turns into an InMail). InMail only happens for an explicit INMAIL action
(or AUTO with inmail explicitly enabled).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from .decision import PageState, decide_action
from .models import ActionType, ItemStatus, NOTE_LIMITED_ACTIONS
from .names import split_full_name
from .settings import BrowserConfig, Settings
from .templating import render

CHAT_OVERLAY_SELECTOR = "div._34a12934"


@dataclass
class ProfileResult:
    status: ItemStatus
    detail: str = ""
    screenshot_path: str = ""
    action_executed: str = ""
    captured_name: str = ""
    degree: str = ""
    headline: str = ""
    location: str = ""
    trace: list[str] = field(default_factory=list)


class LinkedInRunner:
    def __init__(self, settings: Settings, operator: str):
        self.settings = settings
        self.operator = operator
        self.browser_cfg: BrowserConfig = settings.browser
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._pw = await async_playwright().start()

        if self.browser_cfg.cdp_url:
            self._browser = await self._pw.chromium.connect_over_cdp(self.browser_cfg.cdp_url)
            self._context = (
                self._browser.contexts[0]
                if self._browser.contexts
                else await self._browser.new_context()
            )
        else:
            profile_dir = self.settings.profile_path(self.operator)
            launch_kwargs: dict = {
                "user_data_dir": str(profile_dir),
                "headless": self.browser_cfg.headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if self.browser_cfg.channel:
                launch_kwargs["channel"] = self.browser_cfg.channel
            self._context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)

        self._context.set_default_navigation_timeout(self.browser_cfg.nav_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    @staticmethod
    def _on_auth_wall(url: str) -> bool:
        url = (url or "").lower()
        return any(
            marker in url
            for marker in ("/login", "/checkpoint", "/signup", "/authwall", "/uas/login")
        )

    async def open_feed(self) -> None:
        page = self._require_page()
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

    async def logged_in_now(self) -> bool:
        page = self._require_page()
        return not self._on_auth_wall(page.url)

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
            self._context = self._browser = self._page = self._pw = None

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Runner not started. Call start() first.")
        return self._page

    # ---- per-profile processing ------------------------------------------

    async def process(
        self,
        job: dict,
        *,
        action: ActionType,
        dry_run: bool = False,
        send_on_mismatch: bool = False,
        inmail_enabled: bool = False,
        allow_noteless_fallback: bool = False,
        message_if_connected: bool = True,
        gemini=None,
        ai_personalize: bool = False,
        ai_voice: str = "auto",
    ) -> ProfileResult:
        page = self._require_page()
        url = job["linkedin_url"]
        company = (job.get("company") or "").strip()
        message = job.get("message", "")
        email = (job.get("email") or "").strip()
        trace: list[str] = [f"navigate {url}"]

        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            return ProfileResult(ItemStatus.FAILED_OTHER, f"navigation error: {exc}", trace=trace)

        await page.wait_for_timeout(3000)

        if response is not None and response.status == 404:
            trace.append("profile 404")
            return ProfileResult(ItemStatus.FAILED_404, "profile 404", trace=trace)
        if "/404" in page.url or "unavailable" in page.url:
            trace.append("profile unavailable")
            return ProfileResult(ItemStatus.FAILED_404, "profile unavailable", trace=trace)

        await self._hide_chat_overlay(page)
        await self._close_all_message_overlays(page)

        if await self._limit_warning_visible(page):
            trace.append("LinkedIn limit warning visible")
            return ProfileResult(ItemStatus.FAILED_LIMIT, "LinkedIn limit warning detected", trace=trace)

        # 1. Detect the page state.
        state = await self.detect_page_state(page)
        trace.append(
            f"detected degree={state.degree} connect={state.connect} "
            f"message_button={state.has_message_button} pending={state.pending} "
            f"name='{state.profile_name}'"
        )
        if state.headline:
            trace.append(f"headline: {state.headline}")

        # Soft company verification.
        if company:
            matched = await self._company_matches(page, company)
            trace.append(f"company match for '{company}': {matched}")
            if not matched and not send_on_mismatch:
                shot = await self._screenshot(page, job, "mismatch")
                return ProfileResult(
                    ItemStatus.MISMATCH_FLAGGED,
                    f"current company does not match '{company}'",
                    shot, degree=state.degree, captured_name=state.profile_name,
                    headline=state.headline, location=state.location, trace=trace,
                )

        # 2. Decide.
        decision = decide_action(
            action, state,
            inmail_enabled=inmail_enabled,
            message_if_connected=message_if_connected,
            has_message=bool(message.strip()),
        )
        trace.extend(decision.trace)

        if decision.terminal_status is not None:
            shot = await self._screenshot(page, job, decision.terminal_status.value)
            return ProfileResult(
                decision.terminal_status, decision.detail, shot,
                degree=state.degree, captured_name=state.profile_name,
                headline=state.headline, location=state.location, trace=trace,
            )

        executed = decision.action  # ActionType

        # Re-render the message from the real profile name when the row's name was
        # only a URL guess (or missing). The live page is the most reliable source.
        message = self._maybe_rerender(job, state, executed, message, trace)

        # Optional: AI-personalize the message using the captured profile context.
        if (ai_personalize and gemini is not None and getattr(gemini, "available", False)
                and executed in {ActionType.CONNECT_NOTE, ActionType.MESSAGE, ActionType.INMAIL}
                and message.strip()):
            message = await self._ai_personalize(
                gemini, job, state, executed, message, trace, ai_voice,
                custom_gemini_key=kwargs.get("custom_gemini_key"),
                custom_gemini_model=kwargs.get("custom_gemini_model"),
            )

        # 3. Dry run: stop before any send.
        if dry_run:
            trace.append(f"DRY RUN: would execute {executed.value} (no send)")
            shot = await self._screenshot(page, job, "dry_run")
            return ProfileResult(
                ItemStatus.DRY_RUN,
                f"dry run: would {executed.value}",
                shot, action_executed=executed.value,
                degree=state.degree, captured_name=state.profile_name,
                headline=state.headline, location=state.location, trace=trace,
            )

        # 4. Execute.
        if executed == ActionType.CONNECT:
            res = await self._do_connect(page, message, email, job, trace, with_note=False,
                                         allow_noteless_fallback=True)
        elif executed == ActionType.CONNECT_NOTE:
            res = await self._do_connect(page, message, email, job, trace, with_note=True,
                                         allow_noteless_fallback=allow_noteless_fallback)
        elif executed == ActionType.MESSAGE:
            res = await self._send_direct_message(page, message, job, trace, is_inmail=False)
        elif executed == ActionType.INMAIL:
            res = await self._send_direct_message(page, message, job, trace, is_inmail=True)
        else:
            res = ProfileResult(ItemStatus.FAILED_OTHER, f"unhandled action {executed}", trace=trace)

        res.action_executed = executed.value
        res.degree = state.degree
        res.captured_name = state.profile_name or res.captured_name
        res.headline = state.headline
        res.location = state.location
        return res

    async def _ai_personalize(self, gemini, job: dict, state: PageState,
                              executed: ActionType, message: str, trace: list[str],
                              voice: str = "auto", custom_gemini_key: str | None = None,
                              custom_gemini_model: str | None = None) -> str:
        note_limit = 300 if executed in NOTE_LIMITED_ACTIONS else None
        first = (job.get("first_name") or "").strip()
        if not first and state.profile_name:
            first, _ = split_full_name(state.profile_name)
        context = {
            "first_name": first,
            "full_name": state.profile_name or job.get("full_name", ""),
            "headline": state.headline,
            "role": job.get("role", ""),
            "company": job.get("company", ""),
            "location": state.location,
        }
        try:
            tailored = await asyncio.to_thread(
                gemini.tailor_message,
                base=message, context=context,
                max_chars=note_limit, sender=self._sender_first_name(),
                is_note=(executed == ActionType.CONNECT_NOTE), voice=voice,
                api_key=custom_gemini_key, model_override=custom_gemini_model,
            )
        except Exception as exc:  # noqa: BLE001
            trace.append(f"AI personalization failed ({exc}); kept base message")
            return message
        tailored = (tailored or "").strip()
        if not tailored:
            trace.append("AI personalization returned empty; kept base message")
            return message
        if note_limit and len(tailored) > note_limit:
            trace.append(f"AI personalization too long ({len(tailored)} chars); kept base message")
            return message
        trace.append("AI-personalized the message from the profile context")
        return tailored

    def _sender_first_name(self) -> str:
        op = self.settings.operators.get(self.operator)
        full = (op.label if op else self.operator) or self.operator
        return full.split()[0] if full else ""

    def _maybe_rerender(self, job: dict, state: PageState, executed: ActionType,
                        message: str, trace: list[str]) -> str:
        """When the name came from a URL guess (or is missing), rebuild the message
        from the profile's real name read off the page."""
        if job.get("name_source") == "csv":
            return message
        if not state.profile_name:
            return message
        body = job.get("template_body") or ""
        if not body:
            return message
        first, last = split_full_name(state.profile_name)
        variables = dict(job.get("variables") or {})
        variables["first_name"] = first
        variables["last_name"] = last
        variables["full_name"] = state.profile_name
        note_limit = 300 if executed in NOTE_LIMITED_ACTIONS else None
        rendered, issues = render(body, variables, note_limit)
        if not issues and rendered:
            trace.append(f"re-rendered message from profile name '{state.profile_name}'")
            return rendered
        if issues:
            trace.append(f"kept preview message (re-render issues: {', '.join(issues)})")
        return message

    # ---- page-state detection --------------------------------------------

    async def detect_page_state(self, page: Page) -> PageState:
        """Read the profile top card: degree badge, action buttons, pending state.

        Scoped to the left/main column (x < 800) so the right-rail "More profiles
        for you" suggestions (which also show 2nd/3rd badges and Message/Connect
        buttons) never pollute detection. Uses accessible text/aria, not fixed
        coordinates.
        """
        try:
            info = await page.evaluate(
                """() => {
                    const out = {degree:'unknown', connect:'none', hasMessage:false,
                                 pending:false, name:'', follow:false, headline:'', location:''};
                    const main = document.querySelector('main') || document.body;
                    const h1 = main.querySelector('h1');
                    const hr = h1 ? h1.getBoundingClientRect() : null;
                    if (h1) out.name = h1.textContent.trim();
                    const vis = (el) => {
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 1 && r.height > 1;
                    };
                    const degRe = /^(?:\\u00b7\\s*)?(1st|2nd|3rd)\\+?$/;
                    // Degree badge sits next to the name (same row, left column).
                    if (hr) {
                        for (const el of Array.from(main.querySelectorAll('span, div'))) {
                            if (el.children.length) continue;
                            const tx = (el.textContent || '').trim();
                            const mm = tx.match(degRe);
                            if (!mm) continue;
                            const r = el.getBoundingClientRect();
                            if (Math.abs(r.y - hr.y) < 90 && r.x < 800) { out.degree = mm[1]; break; }
                        }
                    }
                    // Headline + location: leaf text lines just below the name, left column.
                    if (hr) {
                        const lines = [];
                        for (const el of Array.from(main.querySelectorAll('div, span'))) {
                            if (el.children.length) continue;
                            const tx = (el.textContent || '').trim();
                            if (!tx || tx.length < 2 || tx.length > 220) continue;
                            if (degRe.test(tx)) continue;
                            if (/^(Connect|Message|More|Follow|Pending|Contact info|Following)$/i.test(tx)) continue;
                            if (tx === out.name) continue;
                            const r = el.getBoundingClientRect();
                            if (r.x < 760 && r.y > hr.y + 4 && r.y < hr.y + 150) {
                                lines.push({ y: r.y, tx });
                            }
                        }
                        lines.sort((a, b) => a.y - b.y);
                        const seen = new Set();
                        const uniq = [];
                        for (const l of lines) { if (!seen.has(l.tx)) { seen.add(l.tx); uniq.push(l.tx); } }
                        if (uniq[0]) out.headline = uniq[0];
                        // Location: a following line that looks geographic or contains a comma.
                        for (let i = 1; i < uniq.length; i++) {
                            const t = uniq[i];
                            if (t === out.headline) continue;
                            if (t.includes(',') || /\\b(United States|India|Sweden|Area|Region|County|Kingdom)\\b/.test(t)) {
                                out.location = t; break;
                            }
                        }
                    }
                    const yTop = hr ? hr.y - 20 : 200;
                    const yBot = hr ? hr.y + 340 : 720;
                    let directConnect = false, more = false;
                    for (const el of Array.from(main.querySelectorAll('button, a'))) {
                        if (!vis(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.x >= 800) continue;             // skip right sidebar
                        if (r.y < yTop || r.y > yBot) continue;
                        const label = (el.getAttribute('aria-label') || '').trim();
                        const text = (el.textContent || '').trim();
                        if (/premium/i.test(label)) continue;
                        if (text === 'Connect' || label === 'Connect' ||
                            /^Invite .* to connect/i.test(label)) directConnect = true;
                        else if (text === 'Message' || label === 'Message' ||
                                 /^Message\\b/.test(label)) out.hasMessage = true;
                        else if (text === 'Pending' || label === 'Pending' ||
                                 /^Pending/i.test(label)) out.pending = true;
                        else if (text === 'More' || label === 'More' ||
                                 /^More actions/i.test(label)) more = true;
                        else if (text === 'Follow' || label === 'Follow') out.follow = true;
                    }
                    out.connect = directConnect ? 'direct' : (more ? 'more_present' : 'none');
                    return out;
                }"""
            )
        except Exception:
            return PageState()

        state = PageState(
            degree=info.get("degree", "unknown"),
            connect=info.get("connect", "none"),
            has_message_button=bool(info.get("hasMessage")),
            pending=bool(info.get("pending")),
            profile_name=info.get("name", "") or "",
            headline=info.get("headline", "") or "",
            location=info.get("location", "") or "",
        )
        state.would_be_inmail = (state.degree != "1st") and state.has_message_button
        return state

    async def resolve_profile(self, url: str) -> dict:
        """Read-only visit: navigate to a profile and return its detected identity
        (name, headline, degree, location). Used by the 'Resolve names' pre-pass.
        Performs NO actions and sends nothing."""
        page = self._require_page()
        out = {"url": url, "name": "", "headline": "", "degree": "", "location": "", "ok": False}
        try:
            resp = await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"navigation error: {exc}"
            return out
        await page.wait_for_timeout(2500)
        if resp is not None and resp.status == 404:
            out["error"] = "profile 404"
            return out
        await self._hide_chat_overlay(page)
        state = await self.detect_page_state(page)
        out.update({
            "name": state.profile_name, "headline": state.headline,
            "degree": state.degree, "location": state.location,
            "ok": bool(state.profile_name),
        })
        return out

    # ---- connect actions --------------------------------------------------

    async def _do_connect(
        self, page: Page, message: str, email: str, job: dict, trace: list[str],
        *, with_note: bool, allow_noteless_fallback: bool,
    ) -> ProfileResult:
        clicked = await self._click_connect(page)
        if not clicked:
            trace.append("Connect not found (action bar or More menu)")
            shot = await self._screenshot(page, job, "connect_unavailable")
            return ProfileResult(ItemStatus.CONNECT_UNAVAILABLE,
                                 "no Connect option found on this profile", shot, trace=trace)
        trace.append("clicked Connect")
        await page.wait_for_timeout(1500)
        return await self._complete_connect_modal(
            page, message, email, job, trace,
            with_note=with_note, allow_noteless_fallback=allow_noteless_fallback,
        )

    async def _complete_connect_modal(
        self, page: Page, message: str, email: str, job: dict, trace: list[str],
        *, with_note: bool, allow_noteless_fallback: bool,
    ) -> ProfileResult:
        await page.wait_for_timeout(1000)

        if await self._dialog_needs_email(page):
            trace.append("invite modal requires email")
            if not email:
                await page.keyboard.press("Escape")
                shot = await self._screenshot(page, job, "email_required")
                return ProfileResult(ItemStatus.FAILED_EMAIL_REQUIRED,
                                     "email required but none provided", shot, trace=trace)
            await self._fill_email(page, email)
            await page.wait_for_timeout(600)

        if not with_note:
            sent = (
                await self._click_role_button(page, "Send without a note")
                or await self._click_role_button(page, "Send invitation")
                or await self._click_role_button(page, "Send")
            )
            if not sent:
                await page.keyboard.press("Enter")
            trace.append("sent connection request without a note")
            await page.wait_for_timeout(2500)
            pending = await self._pending_visible(page)
            shot = await self._screenshot(page, job, "sent" if pending else "no_pending")
            if pending:
                return ProfileResult(ItemStatus.SENT, "connection request sent (no note)", shot, trace=trace)
            return ProfileResult(ItemStatus.FAILED_OTHER,
                                 "invite submitted but Pending not confirmed", shot, trace=trace)

        # With note: reveal the note field, fill it, then Send invitation.
        await self._click_role_button(page, "Add a note")
        await page.wait_for_timeout(1200)
        note_ok = await self._fill_note(page, message)
        trace.append(f"note field filled: {note_ok}")

        if note_ok:
            sent = (
                await self._click_role_button(page, "Send invitation")
                or await self._click_role_button(page, "Send")
            )
            if not sent:
                await page.keyboard.press("Enter")
            trace.append("clicked Send invitation (with note)")
            await page.wait_for_timeout(2500)
            pending = await self._pending_visible(page)
            shot = await self._screenshot(page, job, "sent" if pending else "no_pending")
            if pending:
                return ProfileResult(ItemStatus.SENT, "connection request sent with note", shot, trace=trace)
            return ProfileResult(ItemStatus.FAILED_OTHER,
                                 "invite submitted but Pending not confirmed", shot, trace=trace)

        # Note field unavailable.
        if not allow_noteless_fallback:
            trace.append("note field unavailable and noteless fallback disabled -> flagged")
            await page.keyboard.press("Escape")
            shot = await self._screenshot(page, job, "note_unavailable")
            return ProfileResult(ItemStatus.NEEDS_ATTENTION,
                                 "could not add a note and noteless fallback is off", shot, trace=trace)
        sent = (
            await self._click_role_button(page, "Send without a note")
            or await self._click_role_button(page, "Send invitation")
            or await self._click_role_button(page, "Send")
        )
        if not sent:
            await page.keyboard.press("Enter")
        trace.append("note unavailable -> sent without a note (fallback allowed)")
        await page.wait_for_timeout(2500)
        pending = await self._pending_visible(page)
        shot = await self._screenshot(page, job, "sent_no_note" if pending else "no_pending")
        if pending:
            return ProfileResult(ItemStatus.SENT, "request sent WITHOUT note (note unavailable)", shot, trace=trace)
        return ProfileResult(ItemStatus.FAILED_OTHER,
                             "could not add note and plain invite not confirmed", shot, trace=trace)

    # ---- helpers ----------------------------------------------------------

    async def _hide_chat_overlay(self, page: Page) -> None:
        try:
            await page.evaluate(
                """() => {
                    const o = document.querySelector('div._34a12934');
                    if (o) o.style.display = 'none';
                }"""
            )
        except Exception:
            pass

    async def _limit_warning_visible(self, page: Page) -> bool:
        try:
            found = await page.evaluate(
                """() => {
                    const t = document.body ? document.body.innerText.toLowerCase() : '';
                    return t.includes('you\\'ve reached the weekly invitation limit')
                        || t.includes('weekly invitation limit')
                        || t.includes('reached the monthly limit')
                        || t.includes('you have reached the limit');
                }"""
            )
            return bool(found)
        except Exception:
            return False

    async def _company_matches(self, page: Page, csv_company: str) -> bool:
        try:
            header = await page.evaluate(
                """(csvCompany) => {
                    const companyLower = csvCompany.toLowerCase();
                    const els = document.querySelectorAll('*');
                    for (const el of els) {
                        if (el.children.length === 0 && el.offsetParent !== null) {
                            const text = el.textContent.trim();
                            const rect = el.getBoundingClientRect();
                            if ((text.includes(' at ') || text.includes(' @ ')) &&
                                rect.y > 300 && rect.y < 500 && rect.x < 600 && text.length < 100) {
                                if (text.toLowerCase().includes(companyLower))
                                    return true;
                            }
                        }
                    }
                    const links = document.querySelectorAll('a[href*="/company/"]');
                    for (const link of links) {
                        const rect = link.getBoundingClientRect();
                        if (rect.x > 700 && rect.y > 350 && rect.y < 500 &&
                            link.textContent.trim().length > 1) {
                            if (link.textContent.trim().toLowerCase().includes(companyLower))
                                return true;
                        }
                    }
                    return false;
                }""",
                csv_company,
            )
            if header:
                return True
            await page.evaluate(
                """() => {
                    const sections = document.querySelectorAll('section');
                    for (const s of sections) {
                        if (s.textContent.includes('Experience')) {
                            s.scrollIntoView({ block: 'start' });
                            break;
                        }
                    }
                }"""
            )
            await page.wait_for_timeout(1200)
            exp = await page.evaluate(
                """(csvCompany) => {
                    const companyLower = csvCompany.toLowerCase();
                    const sections = document.querySelectorAll('section');
                    let expSection = null;
                    for (const s of sections) {
                        const h = s.querySelector('h2, [class*="header"]');
                        if (h && h.textContent.trim().includes('Experience')) { expSection = s; break; }
                    }
                    if (!expSection) return false;
                    const lines = expSection.innerText.split('\\n').filter(l => l.trim());
                    for (let i = 0; i < lines.length; i++) {
                        if (lines[i].includes('Present')) {
                            const ctx = lines.slice(Math.max(0, i - 5), i + 5).join(' ').toLowerCase();
                            if (ctx.includes(companyLower)) return true;
                        }
                    }
                    return false;
                }""",
                csv_company,
            )
            await page.evaluate("() => window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)
            return bool(exp)
        except Exception:
            return True

    async def _click_connect(self, page: Page) -> bool:
        """Click a directly visible Connect, else open More and click Connect there.

        Prefers accessible-role clicks; falls back to coordinate clicks only as a
        last resort. Returns False when Connect exists nowhere (out of network).
        """
        # Accessible direct Connect (top card).
        if await self._click_role_button(page, "Connect", timeout_ms=2500):
            return True
        direct = await page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('button, a'));
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    if (el.textContent.trim() !== 'Connect') continue;
                    const r = el.getBoundingClientRect();
                    if (r.y > 200 && r.y < 700 && r.x < 800) {
                        return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
                    }
                }
                return null;
            }"""
        )
        if direct:
            await page.mouse.click(direct["x"], direct["y"])
            return True

        # Open the More dropdown.
        more = await page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('button, a'));
                for (const el of els) {
                    if (el.offsetParent === null) continue;
                    const t = el.textContent.trim();
                    const lab = (el.getAttribute('aria-label') || '').trim();
                    if (t !== 'More' && lab !== 'More' && !/^More actions/i.test(lab)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.y > 200 && r.y < 700 && r.x < 800) {
                        return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
                    }
                }
                return null;
            }"""
        )
        if not more:
            return False
        await page.mouse.click(more["x"], more["y"])
        await page.wait_for_timeout(900)

        # Connect inside the dropdown, anchored on dropdown-only marker items.
        dropdown_connect = await page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('*'));
                const markers = ['Send profile in a message', 'Save to PDF', 'Report / Block', 'About this member'];
                let dropdownX = null;
                for (const el of els) {
                    if (el.children.length === 0 && el.offsetParent !== null &&
                        markers.includes(el.textContent.trim())) {
                        dropdownX = el.getBoundingClientRect().x;
                        break;
                    }
                }
                if (dropdownX === null) return null;
                for (const el of els) {
                    if (el.children.length === 0 && el.textContent.trim() === 'Connect' &&
                        el.offsetParent !== null) {
                        const r = el.getBoundingClientRect();
                        if (Math.abs(r.x - dropdownX) < 60) {
                            return { x: Math.round(r.x + 30), y: Math.round(r.y + 10) };
                        }
                    }
                }
                return null;
            }"""
        )
        if not dropdown_connect:
            await page.keyboard.press("Escape")
            return False
        await page.mouse.click(dropdown_connect["x"], dropdown_connect["y"])
        return True

    # ---- direct messaging / InMail (composer) ----------------------------

    async def _click_message(self, page: Page) -> bool:
        coords = await page.evaluate(
            """() => {
                const vis = (el) => {
                    const st = window.getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 1 && r.height > 1;
                };
                const cands = [];
                for (const el of Array.from(document.querySelectorAll('button, a'))) {
                    if (!vis(el)) continue;
                    const label = (el.getAttribute('aria-label') || '').trim();
                    const text = (el.textContent || '').trim();
                    if (label.toLowerCase().includes('premium')) continue;
                    const isMessage =
                        text === 'Message' ||
                        label === 'Message' ||
                        /^Message\\b/.test(label);
                    if (!isMessage) continue;
                    const r = el.getBoundingClientRect();
                    if (r.y > 250 && r.y < 900 && r.x < 760) {
                        cands.push({ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), top: r.y });
                    }
                }
                if (!cands.length) return null;
                cands.sort((a, b) => a.top - b.top);
                return cands[0];
            }"""
        )
        if not coords:
            return False
        await page.mouse.click(coords["x"], coords["y"])
        return True

    async def _send_direct_message(
        self, page: Page, message: str, job: dict, trace: list[str], *, is_inmail: bool = False,
    ) -> ProfileResult:
        kind = "InMail" if is_inmail else "message"
        recipient = (job.get("full_name") or job.get("first_name") or "").strip()
        first_name = (job.get("first_name") or "").strip()
        needle = message[:40]

        await self._close_all_message_overlays(page)
        await page.wait_for_timeout(1500)

        if not await self._click_message(page):
            shot = await self._screenshot(page, job, "no_message_btn")
            return ProfileResult(ItemStatus.FAILED_NO_CONNECT,
                                 f"no Message button found for {kind}", shot, trace=trace)
        trace.append(f"clicked Message (composing {kind})")
        await page.wait_for_timeout(2500)

        target = await self._target_composer(page, recipient, first_name, timeout_ms=20000)
        if target is None:
            shot = await self._screenshot(page, job, "no_composer")
            return ProfileResult(ItemStatus.FAILED_OTHER,
                                 f"no composer open after clicking Message ({kind})", shot, trace=trace)
        scope, composer, matched, n_composers = target

        if not matched and n_composers > 1:
            shot = await self._screenshot(page, job, "ambiguous_conversation")
            return ProfileResult(
                ItemStatus.FAILED_OTHER,
                f"could not confirm conversation is '{recipient}' with {n_composers} open (skipped to avoid mis-send)",
                shot, trace=trace,
            )

        if matched and n_composers > 1:
            await self._close_other_bubbles(page, recipient, first_name)
            await page.wait_for_timeout(1200)
            target = await self._target_composer(page, recipient, first_name, timeout_ms=8000)
            if target is None:
                shot = await self._screenshot(page, job, "no_composer")
                return ProfileResult(ItemStatus.FAILED_OTHER,
                                     "composer lost after isolating the conversation", shot, trace=trace)
            scope, composer, matched, n_composers = target

        # InMail: fill the subject line if the composer exposes one.
        if is_inmail:
            subject = (message.splitlines()[0] if message.strip() else "Hello")[:60]
            await self._fill_inmail_subject(scope, subject, trace)

        if await self._thread_contains(scope, needle):
            shot = await self._screenshot(page, job, "already_messaged")
            await self._close_all_message_overlays(page)
            status = ItemStatus.INMAIL_SENT if is_inmail else ItemStatus.MESSAGE_SENT
            trace.append("message already present in thread (skipped duplicate)")
            return ProfileResult(status, f"already {kind.lower()}ed previously (skipped duplicate)", shot, trace=trace)

        if not await self._type_into(composer, needle, message):
            shot = await self._screenshot(page, job, "type_failed")
            return ProfileResult(ItemStatus.FAILED_OTHER, f"could not type into {kind} composer", shot, trace=trace)
        await page.wait_for_timeout(800)

        if not await self._click_send_button(scope, composer):
            try:
                await composer.press("Control+Enter")
            except Exception:
                pass
        await page.wait_for_timeout(1500)
        if not await self._message_confirmed(scope, composer, needle):
            try:
                await composer.press("Enter")
            except Exception:
                pass
        await page.wait_for_timeout(2500)

        sent_ok = await self._message_confirmed(scope, composer, needle)
        shot = await self._screenshot(page, job, f"{kind.lower()}_sent" if sent_ok else f"{kind.lower()}_unconfirmed")
        await self._close_all_message_overlays(page)
        trace.append(f"{kind} send confirmed: {sent_ok}")

        if sent_ok:
            status = ItemStatus.INMAIL_SENT if is_inmail else ItemStatus.MESSAGE_SENT
            detail = "InMail sent (non-connection)" if is_inmail else "direct message sent (already connected)"
            return ProfileResult(status, detail, shot, trace=trace)
        return ProfileResult(ItemStatus.FAILED_OTHER, f"{kind} typed but send not confirmed", shot, trace=trace)

    async def _fill_inmail_subject(self, scope, subject: str, trace: list[str]) -> None:
        for sel in ('input[name="subject"]', 'input[id*="subject" i]', 'input[aria-label*="subject" i]'):
            try:
                inp = scope.locator(sel).first
                if await inp.count() and await inp.is_visible():
                    await inp.fill(subject)
                    trace.append("filled InMail subject")
                    return
            except Exception:
                continue

    _COMPOSER_SEL = (
        ".msg-form__contenteditable, "
        'div[role="textbox"][contenteditable="true"]'
    )

    async def _target_composer(self, page: Page, recipient: str, first_name: str, timeout_ms: int = 20000):
        name = (recipient or "").strip().lower()
        fname = (first_name or "").strip().lower()
        elapsed = 0
        step = 700
        while elapsed < timeout_ms:
            for fr in page.frames:
                try:
                    cands = await self._collect_composers(fr)
                except Exception:
                    cands = []
                if not cands:
                    continue
                chosen = None
                matched = False
                for c in cands:
                    h = (c["header"] or "").lower()
                    if name and name in h:
                        chosen, matched = c, True
                        break
                    if fname and len(fname) > 2 and fname in h:
                        chosen, matched = c, True
                        break
                if chosen is None:
                    chosen = max(cands, key=lambda c: c["area"])
                return chosen["scope"], chosen["composer"], matched, len(cands)
            await page.wait_for_timeout(step)
            elapsed += step
        return None

    async def _collect_composers(self, frame) -> list[dict]:
        out: list[dict] = []
        bubbles = frame.locator(".msg-overlay-conversation-bubble")
        try:
            nb = await bubbles.count()
        except Exception:
            nb = 0
        for k in range(nb):
            b = bubbles.nth(k)
            comp = b.locator(self._COMPOSER_SEL).first
            try:
                if await comp.count() == 0 or not await comp.is_visible():
                    continue
            except Exception:
                continue
            out.append(
                {
                    "scope": b,
                    "composer": comp,
                    "header": await self._bubble_header(b),
                    "area": await self._area(comp),
                }
            )
        if out:
            return out
        comps = frame.locator(self._COMPOSER_SEL)
        try:
            nc = await comps.count()
        except Exception:
            nc = 0
        for k in range(nc):
            comp = comps.nth(k)
            try:
                if not await comp.is_visible():
                    continue
            except Exception:
                continue
            out.append(
                {
                    "scope": frame.locator("body"),
                    "composer": comp,
                    "header": await self._frame_header(frame),
                    "area": await self._area(comp),
                }
            )
        return out

    async def _bubble_header(self, bubble) -> str:
        for sel in (
            ".msg-overlay-bubble-header__title",
            ".msg-overlay-bubble-header h2",
            "header h2",
            "h2",
        ):
            try:
                loc = bubble.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    txt = (await loc.inner_text()).strip()
                    if txt:
                        return txt
            except Exception:
                continue
        try:
            for line in (await bubble.inner_text()).split("\n"):
                line = line.strip()
                if line:
                    return line
        except Exception:
            pass
        return ""

    async def _frame_header(self, frame) -> str:
        for sel in (
            ".msg-overlay-bubble-header__title",
            ".msg-overlay-bubble-header h2",
            ".msg-entity-lockup__entity-title",
        ):
            try:
                loc = frame.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    txt = (await loc.inner_text()).strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return ""

    async def _area(self, composer) -> int:
        try:
            box = await composer.bounding_box()
            if box:
                return int(box["width"] * box["height"])
        except Exception:
            pass
        return 0

    async def _type_into(self, composer, needle: str, message: str) -> bool:
        try:
            page = composer.page
        except Exception:
            page = None

        async def landed() -> bool:
            try:
                return needle in (await composer.inner_text())
            except Exception:
                return False

        for _ in range(3):
            try:
                try:
                    await composer.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    await composer.click(timeout=3000)
                except Exception:
                    try:
                        await composer.focus()
                    except Exception:
                        pass
                if page:
                    await page.wait_for_timeout(200)
                try:
                    await composer.fill(message)
                except Exception:
                    pass
                if await landed():
                    return True
                try:
                    await composer.focus()
                except Exception:
                    pass
                if page:
                    try:
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Delete")
                    except Exception:
                        pass
                try:
                    await composer.type(message, delay=15)
                except Exception:
                    pass
                if page:
                    await page.wait_for_timeout(200)
                if await landed():
                    return True
            except Exception:
                pass
            if page:
                await page.wait_for_timeout(600)
        return False

    async def _close_other_bubbles(self, page: Page, recipient: str, first_name: str) -> None:
        name = (recipient or "").strip().lower()
        fname = (first_name or "").strip().lower()

        def is_target(header: str) -> bool:
            h = (header or "").lower()
            if name and name in h:
                return True
            if fname and len(fname) > 2 and fname in h:
                return True
            return False

        close_sel = (
            'button[aria-label^="Close your conversation"], '
            'button[aria-label^="Close conversation"]'
        )
        for _ in range(8):
            closed_any = False
            for fr in page.frames:
                try:
                    bubbles = fr.locator(".msg-overlay-conversation-bubble")
                    nb = await bubbles.count()
                except Exception:
                    nb = 0
                for k in range(nb):
                    b = bubbles.nth(k)
                    try:
                        header = await self._bubble_header(b)
                    except Exception:
                        header = ""
                    if is_target(header):
                        continue
                    try:
                        close = b.locator(close_sel).first
                        if await close.count() and await close.is_visible():
                            await close.click(timeout=2000)
                            closed_any = True
                            await page.wait_for_timeout(500)
                            break
                    except Exception:
                        continue
                if closed_any:
                    break
            if not closed_any:
                break

    async def _click_send_button(self, scope, composer) -> bool:
        for sel in (
            "button.msg-form__send-button",
            "button[type='submit'].msg-form__send-button",
            "button.msg-form__send-btn",
        ):
            try:
                btn = scope.locator(sel)
                n = await btn.count()
            except Exception:
                n = 0
            for i in range(n - 1, -1, -1):
                b = btn.nth(i)
                try:
                    if not await b.is_visible() or not await b.is_enabled():
                        continue
                    await b.click(timeout=3000)
                    return True
                except Exception:
                    continue
        try:
            b = scope.get_by_role("button", name="Send", exact=True).last
            if await b.is_visible() and await b.is_enabled():
                await b.click(timeout=3000)
                return True
        except Exception:
            pass
        return False

    async def _message_confirmed(self, scope, composer, needle: str) -> bool:
        try:
            if needle not in (await composer.inner_text()):
                return True
        except Exception:
            pass
        return await self._thread_contains(scope, needle)

    async def _thread_contains(self, scope, needle: str) -> bool:
        try:
            nodes = scope.locator(
                ".msg-s-event-listitem__body, .msg-s-event-listitem, .msg-s-message-list__event"
            )
            n = await nodes.count()
        except Exception:
            n = 0
        for i in range(n):
            try:
                if needle in (await nodes.nth(i).inner_text()):
                    return True
            except Exception:
                continue
        return False

    async def _close_all_message_overlays(self, page: Page) -> None:
        sel = (
            'button[aria-label^="Close your conversation"], '
            'button[aria-label^="Close conversation"]'
        )
        for _ in range(10):
            closed_any = False
            for fr in page.frames:
                try:
                    btn = fr.locator(sel)
                    n = await btn.count()
                except Exception:
                    n = 0
                for i in range(n):
                    b = btn.nth(i)
                    try:
                        if not await b.is_visible():
                            continue
                        await b.click(timeout=2000)
                        closed_any = True
                        await page.wait_for_timeout(500)
                        break
                    except Exception:
                        continue
                if closed_any:
                    break
            if not closed_any:
                break

    async def _dialog_needs_email(self, page: Page) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """() => {
                        const d = document.querySelector('[role="dialog"], .artdeco-modal');
                        if (!d) return false;
                        return !!d.querySelector(
                            'input[type="email"], input[id*="email" i], input[name*="email" i]'
                        );
                    }"""
                )
            )
        except Exception:
            return False

    async def _fill_email(self, page: Page, email: str) -> bool:
        sel = (
            '[role="dialog"] input[type="email"], '
            '[role="dialog"] input[id*="email" i], '
            '[role="dialog"] input[name*="email" i]'
        )
        try:
            inp = await page.wait_for_selector(sel, timeout=3000, state="visible")
            await inp.click()
            await inp.fill(email)
            return True
        except Exception:
            return False

    async def _fill_note(self, page: Page, message: str) -> bool:
        sel = (
            '#custom-message, textarea[name="message"], '
            '[role="dialog"] textarea'
        )
        try:
            ta = await page.wait_for_selector(sel, timeout=4000, state="visible")
        except Exception:
            ta = None
        if ta is None:
            return False
        probe = message[:20]
        try:
            await ta.click()
            await ta.fill(message)
            await page.wait_for_timeout(300)
            if probe in ((await ta.input_value()) or ""):
                return True
        except Exception:
            pass
        try:
            await ta.click()
            await page.keyboard.type(message, delay=5)
            await page.wait_for_timeout(300)
            return probe in ((await ta.input_value()) or "")
        except Exception:
            return False

    async def _click_role_button(self, page: Page, name: str, timeout_ms: int = 4000) -> bool:
        try:
            loc = page.get_by_role("button", name=name, exact=True)
            count = await loc.count()
        except Exception:
            count = 0
        for i in range(count - 1, -1, -1):
            b = loc.nth(i)
            try:
                if not await b.is_visible():
                    continue
                await b.scroll_into_view_if_needed(timeout=1500)
                await b.click(timeout=timeout_ms)
                return True
            except Exception:
                continue
        return False

    async def _pending_visible(self, page: Page) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """() => {
                        const vis = (el) => {
                            const st = window.getComputedStyle(el);
                            if (st.display === 'none' || st.visibility === 'hidden') return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 1 && r.height > 1;
                        };
                        return Array.from(document.querySelectorAll('button, span'))
                            .some(b => (b.textContent || '').trim() === 'Pending' && vis(b));
                    }"""
                )
            )
        except Exception:
            return False

    async def _screenshot(self, page: Page, job: dict, tag: str) -> str:
        try:
            safe = "".join(c for c in job.get("full_name", "profile") if c.isalnum() or c in "-_ ").strip()
            safe = (safe or "profile").replace(" ", "_")
            idx = job.get("row_index", 0)
            filename = f"{self.operator}_{idx:04d}_{safe}_{tag}.png"
            path = self.settings.screenshots_dir() / filename
            await page.screenshot(path=str(path))
            return str(Path(path).relative_to(self.settings.root))
        except Exception:
            return ""
