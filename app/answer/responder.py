from app.models import IncomingMessage


async def respond(message: IncomingMessage) -> str | None:
    """Return the bot's reply to a message, or None to stay silent.

    Every adapter calls this single entry point. Day 1: echo, to prove the bot is live
    end to end. It will be replaced by the grounded RAG answer.
    """
    if not message.addressed_to_bot:
        return None
    if not message.text:
        return f"Hello {message.author}! I'm Jeli, the group's memory. Ask me anything that was discussed here."
    return f"Hello {message.author}! Jeli is live. You said: {message.text}"
