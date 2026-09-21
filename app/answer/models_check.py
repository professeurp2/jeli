"""At startup, check that the models Jeli is configured with still exist on the key.

Measured (21 Sep): the image models named in the code had been retired and every image request
failed with 404 for a day before anyone looked at the logs. One ListModels call at startup says so
at once, in the logs and on the dashboard.
"""

import logging

log = logging.getLogger(__name__)


async def missing_models(client, wanted: list[str]) -> list[str] | None:
    """The models of `wanted` the key cannot see; None when the list could not be read."""
    try:
        available = set()
        async for model in await client.aio.models.list():
            available.add(str(model.name).removeprefix("models/"))
    except Exception as error:  # a check must never prevent the start
        log.warning("Could not list the Gemini models: %r", error)
        return None
    return [model for model in wanted if model not in available]


async def report_models(client, wanted: list[str]) -> list[str]:
    missing = await missing_models(client, wanted)
    if missing:
        log.error("Models not available on this Gemini key (check the names): %s", ", ".join(missing))
    elif missing is not None:
        log.info("All %d configured Gemini models are available", len(wanted))
    return missing or []
