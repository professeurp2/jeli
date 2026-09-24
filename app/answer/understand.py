"""Understanding a message before answering it: the one door every message goes through.

Measured on the production answer path (19–21 Sep): the routing was a cascade of regular
expressions (catch-up words, recap words, a bare number after a listing, "send me", "in voice",
image verbs…) tried before any understanding, and every misrouted message was fixed with one more
expression. "recap d'aujourd'hui" opened a session recap, "session 4" and "en vocal" searched the
history, "Thank you Jeli" and "jeli" were answered as questions, "how can you help a visually
impaired person?" got "I don't know". One model call now reads the message with the conversation
so far, the community brief and what Jeli can do, and says what the member wants: its kind, the
language, the standalone question, the search queries, the period of a catch-up, the session
named, or the short question to ask back when the request is ambiguous. Greetings, thanks and
slash commands never need the model. When no model answers, the regular expressions remain as
the offline fallback.
"""

import logging
import re
from datetime import datetime, timezone

from pydantic import BaseModel

from app.answer.conversation import CLOSING, Turn
from app.answer.illustrator import asks_for_image, topic_from_request
from app.answer.intents import catchup_since, is_deadlines_request, is_recap_request, looks_like_question
from app.answer.language import TEXTS, detect_language
from app.answer.llm import LLM, LLMUnavailable
from app.answer.persona import CAPABILITIES, PERSONA, background
from app.answer.prompts import LANGUAGES

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
GREETING = re.compile(
    r"^\s*(hi|hello|hey|hiya|good (morning|afternoon|evening)|bonjour|bonsoir|salut|coucou|yo|jeli)"
    r"(\s+(jeli|there|everyone|all))?[\s!.,?👋🙂😊]*$",
    re.IGNORECASE,
)
ABOUT_JELI = re.compile(
    r"^\s*(who|what) are you\b|^\s*what can you do|^\s*what do you do\b|^\s*how do you work|who (made|built|created) you"
    r"|are you a (bot|robot|human|person|real)|^\s*qui es[- ]tu|^\s*tu es qui|c.est quoi jeli|qui est jeli"
    r"|^\s*que (peux|sais)[- ]tu faire|tu (sers|peux servir) (à|a) quoi|comment (tu marches|fonctionnes[- ]tu)"
    r"|qui t.a (créé|fait|construit)|es[- ]tu un (bot|robot|humain)",
    re.IGNORECASE,
)
FILE_REQUEST = re.compile(
    r"\b(send|share|forward|resend|attach|translat\w*|envoie|envoyer|renvoie|partage|transf[eè]re|tradu\w*)\b",
    re.IGNORECASE,
)
SOURCE_REQUEST = re.compile(
    r"^\s*(source|sources|la source|les sources|ta source|tes sources|d.o[uù] (tu|ça) (tiens|vient|sors)[^?]*|where (did you get|is that from|does that come from)[^?]*|source \?)\s*\??\s*$",
    re.IGNORECASE,
)
KINDS = (
    "question", "social", "about_jeli", "catchup", "recap", "session_question", "deadlines", "file",
    "list_documents", "list_sessions", "clarify", "voice", "image", "sources", "vague", "reminder",
    "identity",
)
# Kinds whose reply the model writes itself (no search).
REPLYING_KINDS = ("social", "about_jeli", "clarify", "vague")

SYSTEM = PERSONA + "\n" + CAPABILITIES + """
Your task now: read the latest message a member sent to Jeli, with the conversation so far, and say
what they want. Never answer a question about the community's content yourself: that is done after
you, from the messages. Return:

- "kind", one of:
  "social": greetings, thanks, compliments, small talk, jokes, "can you help me?" without a question.
    Never a request for something Jeli does — a summary, a catch-up, a document, a deadline, a
    reminder. Those have their own kind below, and answering them as small talk means promising
    work that is never done: measured 24 September, "resume moi les chats de ce soir" was answered
    "C'est noté, je te prépare le résumé" and no summary ever came.
  "about_jeli": about Jeli itself — who or what it is, what it can do, how it works, whether it is
    a bot, whether some message or daily summary in the group is Jeli's, how it can help someone.
    NOT questions about the group's content (what was said, who is who in the programme).
  "catchup": what happened or what they missed in the groups over a period (today, this week,
    since Monday, the last 3 days, "recap of the day"). Give "since" (ISO date-time, UTC) when the
    message names the period; leave it empty for "what's new".
  "recap": the summary of one recorded session, class, call or meeting ("recap of the MIT call",
    "summary of yesterday's session", "/recap"). Give "session": the words that name it.
  "session_question": a question about what was said, asked or decided in ONE named session.
    Give "session" too.
  "deadlines": the upcoming deadlines, dates due, what must be submitted when.
  "file": asks to be sent a document or file, or a translated version of one.
  "list_documents" / "list_sessions": which documents, files, recordings or sessions Jeli has.
  "sources": asks where the previous answer came from ("source?", "d'où tu tiens ça ?").
  "voice": only asks for the previous answer again by voice ("en vocal", "say it in a voice note").
  "image": asks for an image or illustration of something (the subject is in "standalone").
  "identity": tells Jeli who they are — their name, their phone number, or both, whether Jeli
    asked for them or they simply said so ("je m'appelle Awa", "mon numéro c'est +223 …", "c'est
    Awa au 93 05 69 36", "here's my number"). Put what they gave in "person_name" and
    "person_number", exactly as written, and leave the other empty when only one was given. A
    number quoted for some other reason — a deadline, an amount, someone else's contact — is not
    this.
  "reminder": asks Jeli to remind them of something later — before a meeting, a session, a
    deadline, at a time ("remind me before the meeting", "rappelle-moi demain à 9 h", "préviens-moi
    une heure avant") — or to cancel a reminder. "standalone" names what and when, from the
    conversation so far.
  "clarify": the request is ambiguous in a way a search cannot settle — two sessions, two
    programmes, an unclear "this"/"ça", a period that could be several — and one short question
    would settle it. Give "reply": the question, one line, offering 2 or 3 options from what you
    know (never a form, never a list of commands). Prefer answering to asking when a reasonable
    reading exists.
  "question": anything else about the programmes, sessions, people, dates, rules, events, what
    someone said.
- "reply": for "social", "about_jeli" and "clarify" only — what Jeli says, in the member's
  language, in the persona above (one to three short sentences; for about_jeli, only the parts of
  its capabilities that answer the member). Otherwise "".
  It is the whole of what Jeli sends: nothing follows it. So it never announces work to come
  ("I'm preparing it", "one moment", "I'll send it shortly"). If the member is asking for
  something Jeli does, the kind is that thing, not "social".
- "standalone": the latest message rewritten as a complete request that makes sense on its own,
  using the conversation so far and the message quoted ("and for the video?" → "What is the
  deadline for the demo video?"; "4" after a numbered list → the fourth item's name), in the
  member's language.
- "queries": for "question", "session_question", "file" and "image": two or three short search
  queries for the group's messages — one in English, one in the member's language if different —
  with the key terms and likely synonyms. Otherwise an empty list.
- "language": the two-letter code of the language the member writes in (fr, en, sw, rw, ln, wo,
  am, pt, es, ar, ha, yo, ig, st…), whatever it is — Jeli answers in every language; and
  "language_name": its name in English ("French", "Sesotho"). A mixed or one-word message takes the
  language of the conversation so far.
- "since": for "catchup" only, the start of the period as an ISO date-time in UTC, computed from
  today's date; "" when not stated.
- "session": for "recap" and "session_question", the words naming the session; "" otherwise.
- "person_name" / "person_number": for "identity" only — the name and the phone number the member
  gave for themselves, as they wrote them. "" otherwise, and "" for whichever they did not give.
"""


class Understood(BaseModel):
    kind: str
    reply: str = ""
    standalone: str = ""
    queries: list[str] = []
    language: str = ""
    language_name: str = ""
    since: str = ""
    session: str = ""
    person_name: str = ""
    person_number: str = ""


def plain(text: str, language: str = "") -> Understood:
    """What Jeli works with when the model cannot help: the message as it is, sorted by the
    regular expressions that routed every message before the understanding step existed."""
    if catchup_since(text) is not None:
        kind = "catchup"
    elif is_deadlines_request(text):
        kind = "deadlines"
    elif is_recap_request(text):
        kind = "recap"
    elif FILE_REQUEST.search(text):
        kind = "file"
    else:
        kind = "question"
    return Understood(kind=kind, reply="", standalone=text, queries=[], language=language)


def conversation_text(turns: list[Turn]) -> str:
    lines = []
    for turn in turns:
        reply = "\n".join(line for line in turn.reply.splitlines() if not line.startswith(">"))
        lines += [f"Member: {turn.message}", f"Jeli: {' '.join(reply.split())[:600]}"]
    return "\n".join(lines)


def parse_since(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class Understander:
    def __init__(self, llm: LLM | None):
        self.llm = llm
        self.brief = ""  # the community brief, set by the brief activity
        self.state = None  # async () -> str: Jeli's own state (awareness.state), optional

    async def understand(self, text: str, language: str, turns: list[Turn] = (), member: str = "") -> Understood:
        """`language` is the word-list guess; the model's answer wins when it gives one."""
        texts = TEXTS[language]
        # The member's own words, without the quoted message the responder prepends.
        own = text.rsplit("]\n", 1)[-1] if text.startswith("[Message cité:") else text
        if GREETING.match(own):
            return Understood(kind="social", reply=texts["greeting_reply"], standalone=text, language=language)
        if CLOSING.match(own):
            return Understood(kind="social", reply=texts["thanks_reply"], standalone=text, language=language)
        if SOURCE_REQUEST.match(own):
            return Understood(kind="sources", standalone=text, language=language)
        if asks_for_image(own) and not topic_from_request(own):
            key = "image_coming" if own != text else "image_what"  # a quote is the subject to draw
            return Understood(kind="social", reply=texts[key], standalone=text, language=language)
        if ABOUT_JELI.search(own) and not turns:
            return Understood(kind="about_jeli", reply=texts["about_jeli"], standalone=text, language=language)
        if self.llm is None:
            return plain(text, language)
        state = ""
        if self.state is not None:
            try:
                state = await self.state()
            except Exception:  # the state is a help, never a condition
                log.exception("Could not read Jeli's state")
        context = background(self.brief, state, member)
        today = datetime.now(timezone.utc)
        prompt = (
            f"Today is {today:%A %d %B %Y}, {today:%H:%M} UTC.\n\n"
            + (f"{context}\n\n" if context else "")
            + (f"Conversation so far:\n{conversation_text(list(turns))}\n\n" if turns else "")
            + f"Latest message (the member seems to write in {LANGUAGES.get(language, 'English')}):\n{text}"
        )
        try:
            understood = await self.llm.generate(
                prompt, Understood, system=SYSTEM, timeout=TIMEOUT_SECONDS, temperature=0, attempts=2
            )
        except LLMUnavailable:
            return plain(text, language)
        if understood.kind not in KINDS:
            understood.kind = "question"
        if understood.kind == "vague":
            understood.kind = "clarify"
        # A request for work is never small talk. The prompt says so, and this makes sure of it:
        # `plain` holds the rules that routed every message before the model existed, and they
        # recognise a catch-up, a recap or a deadline question without one. Measured 24 September:
        # "resume moi les chats de ce soir" came back as "social", and Jeli answered "C'est noté,
        # je te prépare le résumé" — a promise nothing would ever keep.
        if understood.kind in ("social", "clarify", "vague"):
            plainly = plain(text, understood.language or language).kind
            if plainly not in ("question", "social", "clarify", "vague"):
                log.info("Read as %s, but this is a %s request: doing it", understood.kind, plainly)
                understood.kind, understood.reply = plainly, ""
        code = understood.language.strip().lower()[:2]
        # Any language the member writes in (Sesotho, Hausa, Portuguese…): the code the model gave
        # is kept, with its name, so that the answer is written in it; only nonsense falls back.
        if len(code) == 2 and code.isalpha():
            understood.language = code
        else:
            understood.language = language if language in LANGUAGES else detect_language(text)
        understood.language_name = " ".join(understood.language_name.split()).title()[:40] or LANGUAGES.get(understood.language, "")
        if understood.kind in REPLYING_KINDS and not understood.reply.strip():
            understood.reply = texts["about_jeli"] if understood.kind == "about_jeli" else texts["greeting_reply"]
        understood.reply = understood.reply.strip()
        understood.standalone = " ".join(understood.standalone.split()) or text
        understood.queries = [" ".join(q.split()) for q in understood.queries if q.strip()][:3]
        log.info(
            "Understood %r as %s (%s): %r %s", text[:80], understood.kind, understood.language, understood.standalone[:120], understood.queries
        )
        return understood
