"""Apply the team's settings (Runtime) to the running components, at startup and after each change."""

from app.answer.citations import ignored_keys, people_names

# Deadlines the team adds by hand are shown as coming from "Team".
BUILT_IN_LABELS = {"team": "Team"}


def apply(state, runtime) -> None:
    ignored = ignored_keys(runtime["ignored_authors"])
    labels = {**BUILT_IN_LABELS, **runtime["chat_labels"]}
    organisers = ignored_keys(runtime["organisers"])
    for name in ("answerer", "catchup", "extractor"):
        component = getattr(state, name, None)
        if component is not None:
            component.ignored = ignored
            component.organisers = organisers
    for name in ("answerer", "catchup", "extractor", "deadlines"):
        component = getattr(state, name, None)
        if component is not None:
            component.chat_labels = labels
    if getattr(state, "answerer", None) is not None:
        state.answerer.min_similarity = runtime["answer_min_similarity"]
        # Organisers listed with their number are shown by name in quotes ("Diane", not "+250 ···55").
        state.answerer.known_names = people_names(runtime["organisers"])
    if getattr(state, "documents", None) is not None:
        state.documents.known_names = people_names(runtime["organisers"])

    responder = getattr(state, "responder", None)
    if responder is not None:
        responder.duplicate_detection = runtime["duplicate_detection"]
        responder.duplicate_min_similarity = runtime["duplicate_min_similarity"]
        responder.uninvited.limit = runtime["duplicate_replies_per_hour"]
        responder.conversations.enabled = runtime["follow_up"]
        responder.conversations.window_minutes = runtime["follow_up_minutes"]

    guard = getattr(state, "guard", None)
    if guard is not None:
        guard.blocked = ignored_keys(runtime["muted_members"])

    whatsapp = getattr(state, "whatsapp", None)
    if whatsapp is not None:
        whatsapp.suspended = runtime.paused
        whatsapp.groups = set(runtime["groups"])
        whatsapp.silent_groups = set(runtime["silent_groups"])
        whatsapp.bot_name = runtime["bot_name"]
        whatsapp.user_limiter.limit = runtime["whatsapp_user_limit"]
        whatsapp.hourly_limiter.limit = runtime["whatsapp_hourly_limit"]
        whatsapp.spacer.min_interval = runtime["whatsapp_min_send_interval_seconds"]
        whatsapp.enabled_images = runtime["enabled.images"]
        whatsapp.enabled_proactive_images = runtime["enabled.proactive_images"]

    for activity in getattr(state, "activities", {}).values():
        activity.wake()  # schedules and on/off switches may have changed
