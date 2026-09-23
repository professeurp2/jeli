"""Jeli steered in plain words, by one person: the super admin.

The team changes settings on the dashboard. The super admin does it from WhatsApp, in a sentence —
"passe sur le moteur de secours", "limite chacun à 20 réponses par jour", "mets-toi en pause" —
because during a demo nobody opens a browser.

Three things keep this safe. The number is read from the server's environment, never from the
repository or a message. The model may only name a setting Jeli already has and a value it already
accepts: `Runtime.update` validates it exactly as the dashboard does, and refuses the rest. And
every change is written to the activity log with the super admin's name, like any other.
"""

import logging
import re

from pydantic import BaseModel

from app.answer.llm import LLM, LLMUnavailable
from app.answer.persona import PERSONA
from app.control.runtime import FIELDS, Runtime

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
# Settings the super admin must not change in a sentence: they decide who Jeli listens to and who
# may command it, and a misheard word there is not a setting to undo but an incident.
NOT_BY_MESSAGE = {"groups", "organisers", "muted_members", "chat_labels"}
ACTIONS = ("pause", "resume", "brief", "memory", "deadlines", "hello", "goodbye", "none")

SYSTEM = PERSONA + """
Your task now: the person writing to you runs this deployment. They are telling you, in their own
words, to change how you work. Turn what they said into ONE of:

- a setting change: `key` is one of the settings listed below and `value` is what they asked for,
  written the way that setting expects (true/false for a switch, a number, one of the choices);
- an action, when they ask you to do something rather than change something: "pause" (stop
  answering everywhere), "resume", "brief" (rewrite what you know of the community now), "memory"
  (index what has just been said), "deadlines" (look for deadlines now), "hello" (introduce
  yourself to the cohort group — they welcome you to it, or ask you to present yourself), or
  "goodbye" (say your farewell there — they tell you the testing is over, or to take your leave);
- nothing at all (`action` = "none", `key` = ""), when they are simply talking to you, asking a
  question, or when you are not sure what they mean. Never guess a setting from a vague sentence.

reply: one short sentence back to them, in their language, saying what you changed or why you did
not. Plain and direct, as to a colleague who knows the system.

The settings you may change, with what each one takes:
{fields}
"""


class Command(BaseModel):
    key: str = ""
    value: str = ""
    action: str = "none"
    reply: str = ""


def _fields_for_prompt() -> str:
    lines = []
    for key, field in FIELDS.items():
        if key in NOT_BY_MESSAGE:
            continue
        if field.kind == "choice":
            what = "one of " + ", ".join(field.choices)
        elif field.kind == "bool":
            what = "true or false"
        elif field.kind in ("int", "float"):
            low = f"{field.minimum:g}" if field.minimum is not None else "?"
            high = f"{field.maximum:g}" if field.maximum is not None else "?"
            what = f"a number between {low} and {high}"
        elif field.kind == "time":
            what = "a time, HH:MM"
        elif field.kind == "schedule":
            what = "a day and a time, e.g. mon 07:00"
        elif field.kind == "list":
            what = "names or numbers, separated by commas"
        else:
            what = field.kind
        lines.append(f"- {key}: {what}")
    return "\n".join(lines)


def is_super_admin(number: str, author_id: str, author: str = "") -> bool:
    """Whether this message comes from the super admin's own number."""
    wanted = re.sub(r"\D", "", number or "")
    if not wanted:
        return False
    # "22393056936:12@s.whatsapp.net" is one number and one device: the device is not part of it.
    who = (author_id or author or "").split("@", 1)[0].split(":", 1)[0]
    digits = re.sub(r"\D", "", who)
    return bool(digits) and digits.endswith(wanted)


class Admin:
    """Turns the super admin's sentences into settings changes Jeli really applies."""

    def __init__(self, runtime: Runtime, llm: LLM | None, activities: dict | None = None, greet=None):
        self.runtime = runtime
        self.llm = llm
        self.activities = activities or {}
        # Posts Jeli's hello or its goodbye in the cohort group (app/jobs/greetings.py). The same
        # two messages the team can send from the dashboard: said once, never repeated.
        self.greet = greet

    async def handle(self, text: str, actor: str = "super admin") -> str | None:
        """What to answer, or None when this was not a command at all."""
        if self.llm is None or not text.strip():
            return None
        try:
            command = await self.llm.generate(
                f"They wrote: {text.strip()[:600]}",
                Command,
                system=SYSTEM.format(fields=_fields_for_prompt()),
                timeout=TIMEOUT_SECONDS,
                temperature=0,
                attempts=2,
            )
        except LLMUnavailable:
            return None  # no model: it is answered as an ordinary message instead
        action = command.action.strip().lower()
        key = command.key.strip()
        reply = " ".join(command.reply.split())[:400]

        if key and key in FIELDS and key not in NOT_BY_MESSAGE:
            try:
                changed = await self.runtime.update({key: command.value}, actor, f"{actor}: {key} = {command.value}")
            except ValueError as error:
                log.warning("Super admin command refused: %s", error)
                return f"Je n'ai pas pu : {error}"
            log.info("Super admin set %s = %r", key, command.value)
            return reply or ("C'est fait." if changed else "C'était déjà le cas.")

        if action in ("pause", "resume"):
            await self.runtime.update({"paused": action == "pause"}, actor, f"{actor}: {action}")
            return reply or ("Je me mets en pause." if action == "pause" else "Me revoilà.")

        if action in ("hello", "goodbye"):
            if self.greet is None:
                return reply or "Je ne peux pas écrire au groupe d'ici."
            outcome = await self.greet(action)
            log.info("Super admin asked for the %s message: %s", action, outcome)
            if outcome == "already sent":
                return "C'est déjà fait — je ne le répète pas."
            if outcome != "sent":
                return "Je n'ai pas pu : aucun groupe n'est configuré."
            return reply or ("Je me présente au groupe." if action == "hello" else "Je fais mes adieux au groupe.")

        if action in ("brief", "memory", "deadlines"):
            activity = self.activities.get({"brief": "brief", "memory": "memory", "deadlines": "deadlines"}[action])
            if activity is None:
                return reply or "Cette tâche n'est pas active ici."
            activity.run_now(actor)
            return reply or "Je m'en occupe tout de suite."

        return None  # not a command: Jeli answers it like any other message
