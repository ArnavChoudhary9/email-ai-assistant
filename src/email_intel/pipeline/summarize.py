from __future__ import annotations

import logging

from pydantic import ValidationError

from email_intel.integrations.openrouter import OpenRouterClient, complete_with_fallback
from email_intel.models import Email, Extraction
from email_intel.pipeline.parse import truncate_for_llm
from email_intel.prompts.extraction import SYSTEM_PROMPT, user_prompt

log = logging.getLogger(__name__)


def extract(
    client: OpenRouterClient,
    email: Email,
    body: str,
    *,
    primary_model: str,
    fallback_model: str | None,
) -> Extraction:
    """Run the LLM extraction step. Returns a validated Extraction."""
    trimmed = truncate_for_llm(body)
    user = user_prompt(
        sender=email.sender,
        subject=email.subject,
        received_at=email.received_at.isoformat(),
        body=trimmed,
    )

    raw = complete_with_fallback(
        client=client,
        primary_model=primary_model,
        fallback_model=fallback_model,
        system=SYSTEM_PROMPT,
        user=user,
    )

    try:
        return Extraction.model_validate(raw)
    except ValidationError as e:
        log.warning("Extraction JSON failed validation (%s); attempting repair", e)
        # One repair shot: tell the model exactly what went wrong.
        repair_user = (
            f"The previous response was invalid: {e}\n"
            f"Original input:\n{user}\n\n"
            "Return corrected JSON, strict schema."
        )
        raw2 = complete_with_fallback(
            client=client,
            primary_model=primary_model,
            fallback_model=fallback_model,
            system=SYSTEM_PROMPT,
            user=repair_user,
        )
        return Extraction.model_validate(raw2)
