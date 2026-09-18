"""The cohort writes in French and English: Jeli answers in the language of the question."""

import re

FRENCH = set(
    """
    le la les un une des du de est sont quel quelle quels quelles quand où comment pourquoi qui quoi
    que qu est-ce combien faut il elle nous vous ils elles pour avec dans sur pas mais ou et je tu
    moi mon ma mes ton ta tes notre votre leur leurs ce cette ces été sera date lieu merci bonjour aide
    """.split()
)
ENGLISH = set(
    """
    the a an is are was were what when where how why who which whom whose do does did can could will
    would should to of in on at for with from about my your our their this that these those deadline
    hello hi thanks please
    """.split()
)


def detect_language(text: str) -> str:
    """'fr' or 'en'. Short or mixed texts default to English, the cohort's common language."""
    words = re.findall(r"[\w'’-]+", text.lower())
    french = sum(word in FRENCH for word in words) + len(re.findall(r"[éèêàùçôîœ]", text.lower()))
    english = sum(word in ENGLISH for word in words)
    return "fr" if french > english else "en"


TEXTS = {
    "en": {
        "dont_know": "I don't have that in the group's records. Ask the organisers, or rephrase your question.",
        "sources": "Sources",
        "fallback": "I can't write a full answer right now, but here is where the group talked about it:",
        "not_ready": "I'm not connected to the group's memory yet. Try again soon!",
        "help": (
            "Hi, I'm Jeli, the group's memory. Ask me about anything discussed in the group — "
            "mention me (@Jeli), reply to one of my messages, or start with \"Jeli,\". "
            "I answer with my sources, and I say so when I don't know."
        ),
    },
    "fr": {
        "dont_know": "Je n'ai pas cette information dans les échanges du groupe. Demandez aux organisateurs, ou reformulez la question.",
        "sources": "Sources",
        "fallback": "Je ne peux pas rédiger de réponse complète pour l'instant, mais voici où le groupe en a parlé :",
        "not_ready": "Je ne suis pas encore connecté à la mémoire du groupe. Réessayez bientôt !",
        "help": (
            "Bonjour, je suis Jeli, la mémoire du groupe. Posez-moi une question sur ce qui s'est dit ici — "
            "mentionnez-moi (@Jeli), répondez à l'un de mes messages, ou commencez par « Jeli, ». "
            "Je réponds avec mes sources, et je le dis quand je ne sais pas."
        ),
    },
}
