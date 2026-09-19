"""Understanding a message before answering it.

Measured on the production answer path (19 Sep): Jeli answered 19 of 22 real questions the group
had answered, yet looked "not intelligent" to testers — "who are you?", "what can you do?" and
"bonjour, tu peux m'aider ?" got "I don't know", "thanks" got a cited source, "what happened today?"
was searched instead of summarised, and "and for the video?" lost the conversation. One light model
call now reads the message with the conversation so far: small talk gets a human reply, a follow-up
is rewritten as a standalone question, and the question becomes several search queries (English and
the member's language, key terms and synonyms), so a French question finds English messages.
"""

import logging
import re

from pydantic import BaseModel

from app.answer.conversation import CLOSING, Turn
from app.answer.intents import looks_like_question
from app.answer.language import TEXTS
from app.answer.llm import LLM, LLMUnavailable
from app.answer.prompts import LANGUAGES, PROGRAMMES

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 4
GREETING = re.compile(
    r"^\s*(hi|hello|hey|hiya|good (morning|afternoon|evening)|bonjour|bonsoir|salut|coucou|yo)(\s+(jeli|there|everyone|all))?[\s!.,👋🙂😊]*$",
    re.IGNORECASE,
)
ABOUT_JELI = re.compile(
    r"^\s*(who|what) are you\b|what can you do|what do you do\b|how do you work|who (made|built|created) you"
    r"|are you a (bot|robot|human|person|real)|qui es[- ]tu|tu es qui|c.est quoi jeli|qui est jeli"
    r"|que (peux|sais)[- ]tu faire|tu (sers|peux servir) (à|a) quoi|comment (tu marches|fonctionnes[- ]tu)"
    r"|qui t.a (créé|fait|construit)|es[- ]tu un (bot|robot|humain)",
    re.IGNORECASE,
)
FILE_REQUEST = re.compile(
    r"\b(send|share|forward|resend|attach|translat\w*|envoie|envoyer|renvoie|partage|transf[eè]re|tradu\w*)\b",
    re.IGNORECASE,
)
KINDS = ("question", "social", "about_jeli", "catchup", "file")

SYSTEM = f"""\
You read the messages members send to Jeli, the memory assistant of a WhatsApp community: the UniPods
METI AI Innovation Programme, cohort 1. Jeli answers from what was said in the community's groups,
call recordings and shared documents; it also gives catch-ups (/catchup), session recaps (/recap),
upcoming deadlines (/deadlines), searches (/search), and sends the documents shared in the groups,
translated if asked. Team Jeli built it for the community's chatbot hackathon.
{PROGRAMMES}

Classify the latest message ("kind"):
- "social": greetings, thanks, compliments, small talk, jokes, "can you help me?" without a question.
  reply: one or two short, warm sentences in the member's language; after a greeting or an offer of
  help, invite them to ask about the programme, the sessions or the deadlines.
- "about_jeli": about Jeli itself — who or what it is, what it can do, how it works, who made it.
  reply: two or three short sentences on what Jeli does, in the member's language.
- "catchup": what happened or what they missed in the groups over a period (today, this week, since
  Monday). Not what was said in a given session, meeting, class or call: that is a "question".
- "file": asks to be sent a document or file, or a translated version of one.
- "question": anything else, even vague — the programmes, sessions, people, dates, rules, events.
standalone: the latest message rewritten as a complete question that makes sense on its own, using
the conversation so far ("and for the video?" → "What is the deadline for the demo video?"), in the
member's language. queries: for "question" and "file", two or three short search queries for the
group's messages — one in English, one in the member's language if different — with the key terms
and likely synonyms. Otherwise an empty list. Never answer the question; never invent facts.
"""


class Understood(BaseModel):
    kind: str
    reply: str
    standalone: str
    queries: list[str]


def plain(text: str) -> Understood:
    """What Jeli works with when the model cannot help: the message as it is."""
    return Understood(kind="question", reply="", standalone=text, queries=[])


def conversation_text(turns: list[Turn]) -> str:
    lines = []
    for turn in turns:
        reply = "\n".join(line for line in turn.reply.splitlines() if not line.startswith(">"))
        lines += [f"Member: {turn.message}", f"Jeli: {' '.join(reply.split())[:400]}"]
    return "\n".join(lines)


class Understander:
    def __init__(self, llm: LLM | None):
        self.llm = llm

    async def understand(self, text: str, language: str, turns: list[Turn] = ()) -> Understood:
        """Measured: a second model call per message doubled the load on the free Gemini quota, so
        the model is asked only when it helps — a follow-up to understand with the conversation, a
        message that is not a clear question, a request for a file. Greetings, thanks and questions
        about Jeli get their reply without it."""
        texts = TEXTS[language]
        if GREETING.match(text):
            return Understood(kind="social", reply=texts["greeting_reply"], standalone=text, queries=[])
        if CLOSING.match(text):
            return Understood(kind="social", reply=texts["thanks_reply"], standalone=text, queries=[])
        if ABOUT_JELI.search(text):
            return Understood(kind="about_jeli", reply=texts["about_jeli"], standalone=text, queries=[])
        clear_question = looks_like_question(text) and len(text.split()) >= 4
        if self.llm is None or (clear_question and not turns and not FILE_REQUEST.search(text)):
            return plain(text)
        prompt = (
            (f"Conversation so far:\n{conversation_text(list(turns))}\n\n" if turns else "")
            + f"Latest message (the member writes in {LANGUAGES[language]}):\n{text}"
        )
        try:
            understood = await self.llm.generate(
                prompt, Understood, system=SYSTEM, timeout=TIMEOUT_SECONDS, temperature=0, attempts=1
            )
        except LLMUnavailable:
            return plain(text)
        if understood.kind not in KINDS:
            understood.kind = "question"
        if understood.kind == "about_jeli":
            understood.reply = texts["about_jeli"]  # what Jeli does, never a model's guess about it
        elif understood.kind == "social" and not understood.reply.strip():
            understood.reply = texts["greeting_reply"]
        understood.standalone = " ".join(understood.standalone.split()) or text
        understood.queries = [" ".join(q.split()) for q in understood.queries if q.strip()][:3]
        log.info("Understood %r as %s: %r %s", text[:80], understood.kind, understood.standalone[:120], understood.queries)
        return understood
