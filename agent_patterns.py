import random
import time

from groq import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

# Transient failures worth retrying. AuthenticationError, BadRequestError etc.
# are NOT here: retrying them can never help.
RETRYABLE_ERRORS = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
)


def call_groq_with_backoff(client, max_retries=5, base_delay=1.0, **kwargs):
    """Call client.chat.completions.create(**kwargs), retrying transient errors."""
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)
        except RETRYABLE_ERRORS as err:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * 2**attempt + random.uniform(0, 0.5)
            print(f"[retry] {type(err).__name__}, attempt {attempt + 1}/{max_retries}, waiting {delay:.1f}s")
            time.sleep(delay)


def reflect_on_result(tool_name, result):
    """One-line verdict on a tool result, so the model can't miss a failure."""
    if isinstance(result, dict) and result.get("success") is False:
        detail = result.get("stderr") or result.get("detail") or "no further detail available"
        return f"[reflection] {tool_name} FAILED. detail: {str(detail)[:200]}"
    return f"[reflection] {tool_name} succeeded."


def summarize(old, client, model):
    """Ask the model for a short summary of older messages."""
    lines = []
    for m in old:
        text = m.get("content") or ""
        if m.get("tool_calls"):
            names = [tc["function"]["name"] for tc in m["tool_calls"]]
            text += f" [called tools: {', '.join(names)}]"
        lines.append(f"{m.get('role', '?')}: {str(text)[:500]}")

    response = call_groq_with_backoff(
        client,
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Summarize this conversation in under 150 words. Keep the user's "
                    "goals, decisions, event ids, dates, and anything that failed or "
                    "was declined."
                ),
            },
            {"role": "user", "content": "\n".join(lines)},
        ],
    )
    return response.choices[0].message.content


def compress_if_needed(messages, client, model, max_messages=30, keep_recent=10):
    """If the history is long, replace the older part with a summary."""
    if len(messages) <= max_messages:
        return messages

    head = [messages[0]] if messages[0].get("role") == "system" else []
    body = messages[len(head):]

    cut = max(len(body) - keep_recent, 0)
    # Never start the kept part with a tool result: its tool call would be
    # summarized away and the API rejects orphaned tool messages.
    while cut > 0 and body[cut].get("role") == "tool":
        cut -= 1
    if cut == 0:
        return messages

    old, recent = body[:cut], body[cut:]
    summary = summarize(old, client, model)
    summary_msg = {"role": "system", "content": f"Summary of earlier conversation:\n{summary}"}
    return head + [summary_msg] + recent
