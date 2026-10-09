"""Pure decision logic: given the detected page state and the requested action,
decide exactly which outbound action to execute (or which terminal status to
record). No Playwright here, so this is fully unit-testable.

The single most important safety rule lives here: we NEVER escalate to InMail
implicitly. InMail happens only when it is explicitly requested, or when AUTO is
used AND inmail is explicitly enabled in config. This removes the old bug where a
failed Connect-button lookup silently sent an InMail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import ActionType, ItemStatus


@dataclass
class PageState:
    """What the runner observed on the profile page."""

    degree: str = "unknown"          # "1st" | "2nd" | "3rd" | "out_of_network" | "unknown"
    connect: str = "none"            # "direct" | "in_more" | "none"
    has_message_button: bool = False
    pending: bool = False
    profile_name: str = ""
    would_be_inmail: bool = False    # clicking Message would open an InMail composer
    # Enrichment captured from the profile top card (for personalization + records).
    headline: str = ""               # e.g. "CTO at Endra"
    location: str = ""               # e.g. "Stockholm, Sweden"

    @property
    def connected(self) -> bool:
        return self.degree == "1st"

    @property
    def connect_available(self) -> bool:
        # "more_present": a More menu exists and likely holds Connect; the runner
        # confirms by opening it and reports connect_unavailable if it is not there
        # (which is safe -- we flag, we never fall back to InMail).
        return self.connect in {"direct", "more_present", "in_more"}


@dataclass
class Decision:
    """The resolved action plus an audit trail of why."""

    action: ActionType | None = None          # concrete action to execute (None => skip)
    terminal_status: ItemStatus | None = None  # if set, record this and do not execute
    detail: str = ""
    trace: list[str] = field(default_factory=list)


def decide_action(
    requested: ActionType,
    state: PageState,
    *,
    inmail_enabled: bool = False,
    message_if_connected: bool = True,
    has_message: bool = True,
) -> Decision:
    """Resolve the requested action against the observed page state."""
    t: list[str] = [
        f"requested={requested.value}",
        f"degree={state.degree}",
        f"connect={state.connect}",
        f"message_button={state.has_message_button}",
        f"pending={state.pending}",
    ]

    # 1. A request is already outstanding: never re-send.
    if state.pending:
        t.append("decision=pending (invite already outstanding)")
        return Decision(terminal_status=ItemStatus.PENDING_EXISTING,
                        detail="request already pending", trace=t)

    # 2. Already a 1st-degree connection.
    if state.connected:
        if requested in {ActionType.MESSAGE, ActionType.AUTO, ActionType.INMAIL}:
            if message_if_connected and has_message:
                t.append("decision=message (already connected; sending DM)")
                return Decision(action=ActionType.MESSAGE,
                                detail="already connected; direct message", trace=t)
            t.append("decision=already_connected (no DM)")
            return Decision(terminal_status=ItemStatus.ALREADY_CONNECTED,
                            detail="already connected", trace=t)
        # CONNECT / CONNECT_NOTE on someone already connected.
        t.append("decision=already_connected (connect requested but already connected)")
        return Decision(terminal_status=ItemStatus.ALREADY_CONNECTED,
                        detail="already connected", trace=t)

    # 3. Not connected (2nd / 3rd / out_of_network / unknown).
    if requested in {ActionType.CONNECT, ActionType.CONNECT_NOTE}:
        if state.connect_available:
            act = requested
            t.append(f"decision={act.value} (Connect available via {state.connect})")
            return Decision(action=act, detail=f"connect via {state.connect}", trace=t)
        t.append("decision=connect_unavailable (no Connect button anywhere)")
        return Decision(terminal_status=ItemStatus.CONNECT_UNAVAILABLE,
                        detail="no Connect option found on this profile", trace=t)

    if requested == ActionType.MESSAGE:
        # Cannot DM a non-connection. Do NOT silently InMail.
        t.append("decision=needs_attention (DM requested but not connected)")
        return Decision(terminal_status=ItemStatus.NEEDS_ATTENTION,
                        detail="not connected; cannot send a direct message (use connect or inmail)",
                        trace=t)

    if requested == ActionType.INMAIL:
        if state.has_message_button:
            t.append("decision=inmail (explicitly requested)")
            return Decision(action=ActionType.INMAIL,
                            detail="InMail to non-connection (explicit)", trace=t)
        t.append("decision=out_of_network (InMail requested but no message control)")
        return Decision(terminal_status=ItemStatus.OUT_OF_NETWORK,
                        detail="no message/InMail control available", trace=t)

    # requested == AUTO and not connected.
    if state.connect_available:
        act = ActionType.CONNECT_NOTE if has_message else ActionType.CONNECT
        t.append(f"decision={act.value} (AUTO: Connect available via {state.connect})")
        return Decision(action=act, detail=f"auto connect via {state.connect}", trace=t)

    # AUTO, no Connect: only InMail if explicitly enabled. Otherwise flag.
    if inmail_enabled and state.has_message_button:
        t.append("decision=inmail (AUTO: no Connect, inmail_enabled=true)")
        return Decision(action=ActionType.INMAIL,
                        detail="auto InMail (out of network, inmail enabled)", trace=t)

    t.append("decision=out_of_network (AUTO: no Connect, inmail disabled)")
    return Decision(terminal_status=ItemStatus.OUT_OF_NETWORK,
                    detail="out of network and InMail not enabled (no action taken)", trace=t)
