"""Live provider adapter. It does not call a network API in this build.

Owner selection of a provider, model, credential and total spending cap is still
required before any billable request. DeepSeek is a candidate, not a selection.
"""

from resolveai.agent.provider import ProviderAction, RunSnapshot
from resolveai.settings import LiveModelBlocked


class LiveProvider:
    prompt_version = "live-not-authorized"

    def next_action(self, snapshot: RunSnapshot) -> ProviderAction:
        del snapshot
        raise LiveModelBlocked(
            "Live model calls are not authorized. The default remains the fake provider "
            "with a zero budget."
        )
