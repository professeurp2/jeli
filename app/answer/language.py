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
        "already_covered": "💡 This was already answered in the group:",
        "catchup_header": "🗓️ Catch-up since {since} ({messages} messages)",
        "catchup_nothing": "Nothing new in the group since {since}.",
        "catchup_unavailable": "{messages} messages since {since}, but I can't summarise them right now. Try again in a few minutes.",
        "catchup_highlights": "📣 Highlights",
        "catchup_decisions": "✅ Decisions",
        "catchup_deadlines": "⏰ Deadlines and dates",
        "catchup_questions": "❓ Still unanswered",
        "catchup_recordings": "🎥 Recorded sessions",
        "deadlines_header": "⏰ Deadlines in the next {days} days",
        "deadlines_none": "No deadline announced for the next {days} days.",
        "deadlines_coming_up": "⏰ Coming up",
        "deadline_in_call": "call",
        "search_header": "🔎 Where the group talked about it:",
        "search_nothing": "I found nothing about that in the group's records.",
        "recap_summary": "📝 Summary",
        "recap_actions": "📋 To do",
        "recap_moments": "⏱️ Key moments",
        "recap_choose": "Which session? Reply with /recap and its number:",
        "recap_unavailable": "I can't write this recap right now. Try again in a few minutes.",
        "help": (
            "Hi, I'm Jeli, the group's memory. Ask me about anything discussed in the group or in "
            "recorded sessions — mention me (@Jeli), reply to one of my messages, or start with \"Jeli,\". "
            "I answer with my sources, and I say so when I don't know.\n\n"
            "/catchup — what you missed (/catchup 3 days, or \"what did I miss since Monday?\")\n"
            "/recap — summary of a recorded session\n"
            "/deadlines — what is due in the next two weeks\n"
            "/search <topic> — where the group talked about it"
        ),
    },
    "fr": {
        "dont_know": "Je n'ai pas cette information dans les échanges du groupe. Demandez aux organisateurs, ou reformulez la question.",
        "sources": "Sources",
        "fallback": "Je ne peux pas rédiger de réponse complète pour l'instant, mais voici où le groupe en a parlé :",
        "not_ready": "Je ne suis pas encore connecté à la mémoire du groupe. Réessayez bientôt !",
        "already_covered": "💡 Cette question a déjà reçu une réponse dans le groupe :",
        "catchup_header": "🗓️ Récap depuis {since} ({messages} messages)",
        "catchup_nothing": "Rien de nouveau dans le groupe depuis {since}.",
        "catchup_unavailable": "{messages} messages depuis {since}, mais je ne peux pas les résumer pour l'instant. Réessayez dans quelques minutes.",
        "catchup_highlights": "📣 À retenir",
        "catchup_decisions": "✅ Décisions",
        "catchup_deadlines": "⏰ Échéances et dates",
        "catchup_questions": "❓ Questions restées sans réponse",
        "catchup_recordings": "🎥 Sessions enregistrées",
        "deadlines_header": "⏰ Échéances des {days} prochains jours",
        "deadlines_none": "Aucune échéance annoncée pour les {days} prochains jours.",
        "deadlines_coming_up": "⏰ À venir",
        "deadline_in_call": "appel",
        "search_header": "🔎 Où le groupe en a parlé :",
        "search_nothing": "Je n'ai rien trouvé à ce sujet dans les échanges du groupe.",
        "recap_summary": "📝 Résumé",
        "recap_actions": "📋 À faire",
        "recap_moments": "⏱️ Moments clés",
        "recap_choose": "Quelle session ? Répondez /recap suivi de son numéro :",
        "recap_unavailable": "Je ne peux pas rédiger ce résumé pour l'instant. Réessayez dans quelques minutes.",
        "help": (
            "Bonjour, je suis Jeli, la mémoire du groupe. Posez-moi une question sur ce qui s'est dit ici "
            "ou pendant les sessions enregistrées — mentionnez-moi (@Jeli), répondez à l'un de mes messages, "
            "ou commencez par « Jeli, ». Je réponds avec mes sources, et je le dis quand je ne sais pas.\n\n"
            "/catchup — ce que vous avez raté (/catchup 3 jours, ou « qu'est-ce que j'ai raté depuis lundi ? »)\n"
            "/recap — résumé d'une session enregistrée\n"
            "/deadlines — les échéances des deux prochaines semaines\n"
            "/search <sujet> — où le groupe en a parlé"
        ),
    },
}
