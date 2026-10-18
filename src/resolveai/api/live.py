"""Owner-started Luna API. The default `resolveai.api.app` import stays on the fake provider.

Start only after `RESOLVEAI_ALLOW_LIVE=1`, `MODEL_PROVIDER=openai`,
`MODEL_NAME=gpt-6-luna`, a positive `LLM_BUDGET_USD`, and `OPENAI_API_KEY`
are present in the local environment. This module does not read a browser flag.
"""

from __future__ import annotations

import uvicorn

from resolveai.api.app import create_api_app
from resolveai.settings import load_owner_live_settings


def main() -> None:
    settings, api_key = load_owner_live_settings()
    app = create_api_app(settings, live_api_key=api_key)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
