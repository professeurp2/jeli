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
        "dont_know": "Hmm, I don't have anything on that in the group's history yet. Best to ask an organiser directly, or try /search to browse what's been shared.",
        "dont_know_near": "I couldn't find a clear answer, but these discussions from the group might help:",
        "greeting_reply": "Hey! 👋 I'm Jeli — your group's memory. I keep track of sessions, announcements, decisions and deadlines. What can I help you with?",
        "thanks_reply": "Anytime! 😊",
        "welcome_new": "Welcome! 🙏 Great to have you here.",
        "session_in_progress": "⏳ «{title}» (shared by {who}, {day}) is still being transcribed — {progress}. Give me a few more minutes and I'll have the full details for you!",
        "about_jeli": (
            "Hey, I'm Jeli 👋 Think of me as the group's memory — I follow everything: chats, call recordings, "
            "and the METI UniPods AI Programme documents (MIT, Wadhwani, Ethiopian AI Institute, Cohort 1). "
            "Ask me anything — I'll tell you where it came from, and I'll always be honest when I don't know."
        ),
        "sources": "Sources",
        "fallback": "Things are a bit busy right now, but here's where the group discussed this — ask me again in a minute for a full answer:",
        "not_ready": "I'm still warming up — give me a moment and try again!",
        "already_covered": "💡 The group already covered this:",
        "catchup_header": "🗓️ Catch-up since {since} ({messages} messages)",
        "catchup_nothing": "All quiet since {since} — nothing new in the groups! ☀️",
        "catchup_not_live": "I'm not in the group yet, so my memory stops on {day} at {time} GMT. Once I'm added, I'll follow everything live and keep you up to date.",
        "catchup_unavailable": "{messages} messages since {since}, but I can't summarise them right now — try again in a few minutes.",
        "catchup_highlights": "📣 Highlights",
        "catchup_decisions": "✅ Decisions",
        "catchup_deadlines": "⏰ Deadlines and dates",
        "catchup_questions": "❓ Still unanswered",
        "catchup_recordings": "🎥 Recorded sessions",
        "deadlines_header": "⏰ Deadlines in the next {days} days",
        "deadlines_none": "Nothing due in the next {days} days — enjoy the breather! 😌",
        "deadlines_coming_up": "⏰ Coming up",
        "deadline_in_call": "call",
        "deadline_in_document": "document",
        "file_here": "📄 Here's «{title}», shared by {who} on {day}.",
        "file_translating": "📄 Translating «{title}» into {language} — I'll drop it here in a minute or two.",
        "file_translated": "«{title}» in {language} (machine translation — the original is the reference)",
        "file_translate_failed": "Sorry, the translation of «{title}» didn't go through this time. Give it another try in a few minutes!",
        "file_language_unsupported": "I can translate documents into {languages}.",
        "search_header": "🔎 Where the group talked about it:",
        "search_nothing": "Nothing came up for that in the group's records — try different keywords maybe?",
        "recap_summary": "📝 Summary",
        "recap_actions": "📋 To do",
        "recap_moments": "⏱️ Key moments",
        "recap_choose": "Which session would you like? Reply with its number (e.g. 4) or /recap 4:",
        "recap_unavailable": "I can't write the recap right now — try again in a few minutes.",
        "help": (
            "Hey, I'm Jeli 👋 I'm the group's memory. Ask me anything — what was decided, what's coming up, "
            "what the METI UniPods AI Programme says about MIT, Wadhwani or the Ethiopian AI Institute. "
            "Just mention me (@Jeli), reply to one of my messages, or start with \"Jeli,\".\n\n"
            "/catchup — catch up on what you missed\n"
            "/recap — get a summary of a recorded session\n"
            "/deadlines — see what's due in the next two weeks\n"
            "/search <topic> — find where the group talked about something"
        ),
        "guard_cooling_down": "🙏 You've sent me quite a few messages in a short time — I need a short breather! I'll be back in about an hour, feel free to ask me then.",
        "guard_oversized": "⚠️ That message is a bit too long for me to handle (limit: ~1 500 characters). Could you shorten it a little?",
        "guard_repeat": "💬 You've sent me this a few times already. If I haven't replied, it's because I don't have that info yet — try rephrasing or use /search.",
        "admin_silenced": "✅ Got it! Going quiet in this group{duration}. Use /resume whenever you want me back.",
        "admin_resumed": "✅ I'm back! All ears as usual. 👑",
        "voice_not_heard": "Hey, I got your voice note but couldn't quite make it out 🎤 Could you try again, or just type your question? I'm here either way!",
        "voice_reply_unavailable": "Couldn't send a voice reply right now — something came up on my end 🎤 Here's my answer in text:",
        "image_what": "What would you like an image of? 🎨 Describe the subject and I'll illustrate it!",
        "image_offer": "📊 I can illustrate that — reply *yes* if you'd like an image!",
    },
    "fr": {
        "dont_know": "Hmm, je n'ai rien trouvé là-dessus dans les échanges du groupe. Tu peux poser la question directement aux organisateurs, ou essayer /search avec un mot-clé.",
        "dont_know_near": "Je n'ai pas trouvé de réponse claire, mais ces discussions du groupe pourraient t'aider :",
        "greeting_reply": "Salut ! 👋 Moi c'est Jeli — la mémoire du groupe. Je suis tout : sessions, annonces, décisions, échéances. Je t'aide avec quoi ?",
        "thanks_reply": "Avec plaisir ! 😊",
        "welcome_new": "Bienvenue ! 🙏 Ravi de t'avoir parmi nous.",
        "session_in_progress": "⏳ «{title}» (partagé par {who}, {day}) est encore en cours de transcription — {progress}. Encore quelques minutes et j'aurai tous les détails pour toi !",
        "about_jeli": (
            "Salut, c'est Jeli 👋 Pense à moi comme à la mémoire du groupe — je suis tout : les échanges, "
            "les sessions enregistrées, et les documents du programme METI UniPods AI (MIT, Wadhwani, "
            "Institut Éthiopien d'IA, Cohorte 1). Pose-moi n'importe quelle question — je te dis d'où ça "
            "vient, et je suis honnête quand je ne sais pas."
        ),
        "sources": "Sources",
        "fallback": "C'est un peu chargé là, mais voici où le groupe en a parlé — redemande-moi dans une minute pour une réponse complète :",
        "not_ready": "Je suis encore en train de me réveiller — donne-moi un instant et réessaie !",
        "already_covered": "💡 Le groupe a déjà répondu à ça :",
        "catchup_header": "🗓️ Récap depuis {since} ({messages} messages)",
        "catchup_nothing": "Tout calme depuis {since} — rien de nouveau dans les groupes ! ☀️",
        "catchup_not_live": "Je ne suis pas encore dans le groupe, donc ma mémoire s'arrête le {day} à {time} GMT. Dès que je suis ajouté, je suis tout en direct et je te tiens à jour.",
        "catchup_unavailable": "{messages} messages depuis {since}, mais je ne peux pas les résumer maintenant — réessaie dans quelques minutes.",
        "catchup_highlights": "📣 À retenir",
        "catchup_decisions": "✅ Décisions",
        "catchup_deadlines": "⏰ Échéances et dates",
        "catchup_questions": "❓ Questions restées sans réponse",
        "catchup_recordings": "🎥 Sessions enregistrées",
        "deadlines_header": "⏰ Échéances des {days} prochains jours",
        "deadlines_none": "Rien d'urgent dans les {days} prochains jours — profite ! 😌",
        "deadlines_coming_up": "⏰ À venir",
        "deadline_in_call": "appel",
        "deadline_in_document": "document",
        "file_here": "📄 Voilà «{title}», partagé par {who} le {day}.",
        "file_translating": "📄 Je traduis «{title}» en {language} — je t'envoie ça d'ici une ou deux minutes.",
        "file_translated": "«{title}» en {language} (traduction automatique — l'original fait foi)",
        "file_translate_failed": "Désolé, la traduction de «{title}» n'a pas fonctionné cette fois. Réessaie dans quelques minutes !",
        "file_language_unsupported": "Je peux traduire les documents en {languages}.",
        "search_header": "🔎 Où le groupe en a parlé :",
        "search_nothing": "Je n'ai rien trouvé à ce sujet dans les échanges du groupe — essaie avec d'autres mots peut-être ?",
        "recap_summary": "📝 Résumé",
        "recap_actions": "📋 À faire",
        "recap_moments": "⏱️ Moments clés",
        "recap_choose": "Quelle session tu veux ? Réponds avec son numéro (ex. 4) ou /recap 4 :",
        "recap_unavailable": "Je ne peux pas rédiger ce résumé là — réessaie dans quelques minutes.",
        "help": (
            "Salut, c'est Jeli 👋 la mémoire du groupe. Pose-moi n'importe quelle question — ce qui a été "
            "décidé, ce qui arrive, ce que dit le programme METI UniPods AI sur MIT, Wadhwani ou l'Institut "
            "Éthiopien d'IA. Mentionne-moi (@Jeli), réponds à un de mes messages, ou commence par « Jeli, ».\n\n"
            "/catchup — rattraper ce que tu as manqué\n"
            "/recap — résumé d'une session enregistrée\n"
            "/deadlines — les prochaines échéances\n"
            "/search <sujet> — trouver où on en a parlé dans le groupe"
        ),
        "guard_cooling_down": "🙏 Tu m'as envoyé pas mal de messages d'un coup — j'ai besoin d'une petite pause ! Je reviens dans environ une heure, n'hésite pas à me reposer ta question à ce moment.",
        "guard_oversized": "⚠️ Ton message est un peu long pour moi (limite : ~1 500 caractères). Tu peux le raccourcir un peu ?",
        "guard_repeat": "💬 Tu m'as déjà envoyé ça plusieurs fois. Si je n'ai pas répondu, c'est que je n'ai pas encore cette info — essaie de reformuler ou utilise /search.",
        "admin_silenced": "✅ Reçu ! Je me fais discret dans ce groupe{duration}. Un /resume quand tu veux me faire revenir.",
        "admin_resumed": "✅ Me revoilà ! J'écoute et réponds normalement. 👑",
        "voice_not_heard": "Hey, j'ai reçu ton vocal mais j'ai pas réussi à le saisir 🎤 Tu peux réessayer, ou juste écrire ta question ? Je suis là dans tous les cas !",
        "voice_reply_unavailable": "Je n'arrive pas à t'envoyer un vocal là — il se passe un truc de mon côté 🎤 Voilà ma réponse en texte :",
        "image_what": "De quoi voudrais-tu une image ? 🎨 Décris le sujet et j'illustre !",
        "image_offer": "📊 Je peux illustrer ça — réponds *oui* si tu veux une image !",
    },
}
