# Exit-node selection for hosted LinkedIn work

Status: approved in conversation on 2026-09-24. This extends the Phase D branch.

## Intent

The operator can choose an approved Tailscale exit node for each manual or queued LinkedIn task. A saved default handles unattended inbound sync and other browser operations. If the selected node is unavailable or its route cannot be verified, no LinkedIn browser operation starts. The agent VPS policy and host stay unchanged.

## Design

Tailscale selects an exit node for the entire LinkBound VM, not an individual process. The existing `RunCoordinator` admits only one browser operation at a time. A route lease around that operation selects the requested node, verifies Tailscale's selected node and public egress, and keeps it fixed until the last browser closes. No second browser task may switch routes during the lease. After the operation, the lease clears the exit node, even after an error. The app, viewer, and SSH remain on tailnet routes. Other non-tailnet traffic from LinkBound shares the selected route while the lease is active.

The UI lists currently approved nodes and clearly shows online status. A saved default can be changed between tasks. Manual runs may override it. Queued campaigns persist a selected node in their existing run options; changing a campaign's route for future chunks is an explicit queue control. Daily inbox sync reads the current default when due. Missing default, offline node, failed route switch, missing public IP, or changed route during a run all stop the browser task without falling back to Linode direct egress or another node. Failed scheduled work remains visible and retryable after the route is repaired.

The app does not receive root privileges. Tailscale's supported Linux `--operator=linkbound` setting lets the existing service user change the local machine's exit-node preference. LinkBound validates the requested ID against approved online nodes before calling `tailscale set`. This operator setting grants that Unix user broader control over its own Tailscale daemon, so it is restricted to the existing non-root service account. No Tailscale policy edits or changes to the agent VPS are part of this feature.

## Evidence and trade-offs

Persist the selected node ID, display name, and observed public IPv4 with each run. Do not treat a residential IP as proof of LinkedIn trust. Switching networks between tasks can trigger LinkedIn security prompts. A paused or unavailable exit node cannot silently become direct egress. The first deployment remains no-send; a live pilot is required before enabling hosted sends or daily inbound again.

## Verification

Unit tests cover node parsing, rejected/offline selections, route switch and cleanup, no browser launch without verified egress, and schedule behavior when the node is unavailable. Browser UI tests cover selecting a node and a blocked state. A live no-send pilot with an approved laptop exit node verifies observed egress, tailnet app/viewer/SSH reachability, and behavior when the laptop becomes unavailable.
