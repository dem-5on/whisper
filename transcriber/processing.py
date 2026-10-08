"""Text transformations; default behavior is intentionally exact pass-through."""

from .config import ProcessingConfig


def process_transcript(text: str, config: ProcessingConfig) -> str:
    if not config.cleanup:
        return text
    processed = " ".join(text.split())
    for old, new in config.replacements.items():
        processed = processed.replace(old, new)
    return processed
