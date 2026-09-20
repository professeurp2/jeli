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
        "dont_know": "I couldn't find that in what the groups have discussed so far. Ask the organisers, or try /search with a keyword.",
        "dont_know_near": "I couldn't find a clear answer in the groups. The closest discussions:",
        "greeting_reply": "Hello! 👋 I'm Jeli, the group's memory. Ask me anything about the programme, the sessions or the deadlines.",
        "thanks_reply": "You're welcome! 🙌",
        "session_in_progress": "⏳ The recording of «{title}» (shared by {who}, {day}) is being transcribed right now — {progress}. Ask me again in a few minutes and I'll tell you what was said.",
        "about_jeli": (
            "I'm Jeli 👋, the memory of this community. I've read the groups, the recorded sessions and the "
            "METI UniPods AI Programme documents — MIT, Wadhwani, the Ethiopian AI Institute, the Cohort 1 "
            "selection data. Ask me anything that was said or shared, and I'll tell you where — or that I "
            "don't know. I can also catch you up (/catchup), recap a session (/recap), list what's due "
            "(/deadlines) and find where a topic was discussed (/search). Team Jeli built me for the hackathon."
        ),
        "sources": "Sources",
        "fallback": "I'm answering a lot of questions right now, so here is where the group talked about it — ask me again in a minute for a full answer:",
        "not_ready": "I'm not connected to the group's memory yet. Try again soon!",
        "already_covered": "💡 This was already answered in the group:",
        "catchup_header": "🗓️ Catch-up since {since} ({messages} messages)",
        "catchup_nothing": "Nothing new in the groups since {since}",
        "catchup_not_live": "I'm not receiving the groups' messages yet: my memory stops on {day} at {time} GMT, so I can't tell you what's new since. As soon as my number is in the group, I'll follow everything live.",
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
        "deadline_in_document": "document",
        "file_here": "📄 Here is «{title}», shared by {who} on {day}.",
        "file_translating": "📄 I'm translating «{title}» into {language}: I'll send it here in a minute or two.",
        "file_translated": "«{title}» in {language} (machine translation: the original prevails)",
        "file_translate_failed": "Sorry, I couldn't translate «{title}» right now. Try again in a few minutes.",
        "file_language_unsupported": "I can translate documents into {languages}.",
        "search_header": "🔎 Where the group talked about it:",
        "search_nothing": "I found nothing about that in the group's records.",
        "recap_summary": "📝 Summary",
        "recap_actions": "📋 To do",
        "recap_moments": "⏱️ Key moments",
        "recap_choose": "Which session? Reply with /recap and its number:",
        "recap_unavailable": "I can't write this recap right now. Try again in a few minutes.",
        "help": (
            "Hi, I'm Jeli, the group's memory. Ask me about anything discussed in the group, in recorded "
            "sessions, or in the METI UniPods AI Programme documents (MIT, Wadhwani, Ethiopian AI Institute, "
            "Cohort 1 selection) — mention me (@Jeli), reply to one of my messages, or start with \"Jeli,\". "
            "I answer with my sources, and I say so when I don't know.\n\n"
            "/catchup — what you missed (/catchup 3 days, or \"what did I miss since Monday?\")\n"
            "/recap — summary of a recorded session\n"
            "/deadlines — what is due in the next two weeks\n"
            "/search <topic> — where the group talked about it"
        ),
        "guard_cooling_down": "🙏 I've received a lot of messages from you in a short time and need a short break. I'll be back in about an hour — feel free to ask again then.",
        "guard_oversized": "⚠️ Your message is too long for me to process (limit: ~1 500 characters). Could you shorten your question?",
        "guard_repeat": "💬 You've already sent me this message several times. If I haven't answered, it's because I don't have that information — try rephrasing or using /search.",
    },
    "fr": {
        "dont_know": "Je n'ai pas trouvé cela dans les échanges des groupes. Demandez aux organisateurs, ou essayez /search avec un mot-clé.",
        "dont_know_near": "Je n'ai pas trouvé de réponse claire dans les groupes. Les discussions les plus proches :",
        "greeting_reply": "Bonjour ! 👋 Je suis Jeli, la mémoire du groupe. Posez-moi vos questions sur le programme, les séances ou les échéances.",
        "thanks_reply": "Avec plaisir ! 🙌",
        "session_in_progress": "⏳ L'enregistrement de «{title}» (partagé par {who}, {day}) est en cours de transcription — {progress}. Redemandez-moi dans quelques minutes et je vous dirai ce qui s'y est dit.",
        "about_jeli": (
            "Je suis Jeli 👋, la mémoire de cette communauté. J'ai lu les groupes, les séances enregistrées et "
            "les documents du programme METI UniPods AI — MIT, Wadhwani, l'Institut Éthiopien d'IA, les données "
            "de sélection de la Cohorte 1. Demandez-moi tout ce qui a été dit ou partagé, je vous dis où — ou "
            "que je ne sais pas. Je fais aussi le point (/catchup), le résumé d'une séance (/recap), la liste "
            "des échéances (/deadlines) et je retrouve où un sujet a été abordé (/search). L'équipe Jeli m'a créé pour le hackathon."
        ),
        "sources": "Sources",
        "fallback": "Je reçois beaucoup de questions en ce moment : voici où le groupe en a parlé — redemandez-moi dans une minute pour une réponse complète :",
        "not_ready": "Je ne suis pas encore connecté à la mémoire du groupe. Réessayez bientôt !",
        "already_covered": "💡 Cette question a déjà reçu une réponse dans le groupe :",
        "catchup_header": "🗓️ Récap depuis {since} ({messages} messages)",
        "catchup_nothing": "Rien de nouveau dans les groupes depuis {since}",
        "catchup_not_live": "Je ne reçois pas encore les messages des groupes : ma mémoire s'arrête le {day} à {time} GMT, donc je ne peux pas vous dire ce qui s'est passé depuis. Dès que mon numéro sera dans le groupe, je suivrai tout en direct.",
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
        "deadline_in_document": "document",
        "file_here": "📄 Voici «{title}», partagé par {who} le {day}.",
        "file_translating": "📄 Je traduis «{title}» en {language} : je l'envoie ici d'ici une ou deux minutes.",
        "file_translated": "«{title}» en {language} (traduction automatique : l'original fait foi)",
        "file_translate_failed": "Désolé, je n'ai pas pu traduire «{title}» pour l'instant. Réessaie dans quelques minutes.",
        "file_language_unsupported": "Je peux traduire les documents en {languages}.",
        "search_header": "🔎 Où le groupe en a parlé :",
        "search_nothing": "Je n'ai rien trouvé à ce sujet dans les échanges du groupe.",
        "recap_summary": "📝 Résumé",
        "recap_actions": "📋 À faire",
        "recap_moments": "⏱️ Moments clés",
        "recap_choose": "Quelle session ? Répondez /recap suivi de son numéro :",
        "recap_unavailable": "Je ne peux pas rédiger ce résumé pour l'instant. Réessayez dans quelques minutes.",
        "help": (
            "Bonjour, je suis Jeli, la mémoire du groupe. Posez-moi une question sur ce qui s'est dit ici, "
            "pendant les sessions enregistrées, ou dans les documents du programme METI UniPods AI (MIT, "
            "Wadhwani, Institut Éthiopien d'IA, sélection Cohorte 1) — mentionnez-moi (@Jeli), répondez à "
            "l'un de mes messages, ou commencez par « Jeli, ». Je réponds avec mes sources, et je le dis quand je ne sais pas.\n\n"
            "/catchup — ce que vous avez raté (/catchup 3 jours, ou « qu'est-ce que j'ai raté depuis lundi ? »)\n"
            "/recap — résumé d'une session enregistrée\n"
            "/deadlines — les échéances des deux prochaines semaines\n"
            "/search <sujet> — où le groupe en a parlé"
        ),
        "guard_cooling_down": "🙏 J'ai reçu beaucoup de messages de ta part en peu de temps et j'ai besoin d'une petite pause. Je serai de retour dans environ une heure — tu pourras me poser ta question à ce moment-là.",
        "guard_oversized": "⚠️ Ton message est trop long pour que je puisse le traiter (limite : ~1 500 caractères). Peux-tu résumer ta question ?",
        "guard_repeat": "💬 Tu m'as déjà envoyé ce message plusieurs fois. Si je n'ai pas répondu, c'est que je n'ai pas cette information — essaie de reformuler ou utilise /search.",
    },
}
