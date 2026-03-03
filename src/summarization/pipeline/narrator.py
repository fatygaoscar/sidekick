"""Pipeline narrative generation using shared cohesive summarizer."""

from typing import Awaitable, Callable, Optional

from src.summarization.cohesive import generate_cohesive_summary

from .types import StructuredItems


LLMCallFunc = Callable[[str, str], Awaitable[str]]


async def generate_narrative(
    items: StructuredItems,
    template: str,
    transcript: str,
    llm_call: LLMCallFunc,
    perspective: Optional[str] = None,
    template_prompt: Optional[str] = None,
    context_length: int = 4096,
) -> tuple[str, float, str, int, str]:
    """Generate narrative and return metadata for pipeline output."""
    narrative, context_mode, passes_used, style_profile = await generate_cohesive_summary(
        llm_call=llm_call,
        transcript=transcript,
        template=template,
        template_contract=template_prompt or "",
        perspective=perspective,
        structured_items=items,
        context_length=context_length,
    )
    return narrative, 1.0, context_mode, passes_used, style_profile
