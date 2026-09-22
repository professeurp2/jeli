# meti_ai vs Jeli — ce que fait le bot le mieux noté, et ce que Jeli fait maintenant

**Source :** export du groupe « UniPods METI AI Program 2026 Cohort » du 4 au 22 septembre 2026 (5 557 lignes). Le bot `+229 01 49 48 62 56` (« meti_bot / meti_ai ») est l'auteur le plus actif du groupe : **181 messages** entre le 18 et le 21 septembre. Jeli n'y apparaît qu'une fois (« Jeli_bot ») : ses tests ont eu lieu dans un autre groupe, la comparaison porte donc sur le comportement observé de meti_ai face aux fonctions de Jeli.

## 1. Ce que meti_ai fait bien (observé dans le groupe)

| Comportement | Exemple observé | Jeli au 22 sept. |
|---|---|---|
| **Récap d'une période nommée** (« what happened this morning ? ») avec les décisions, les reports et les @mentions des organisateurs | 21 sept. 10:19 : tests de bots, créneau manqué = pas de seconde chance, session Ethiopian AI Institute reportée au 30 sept., certificats annoncés par @Gift | Oui : la compréhension donne la période (`since`), le récap est une liste plate avec @mentions. **Bug corrigé** : une nuit calme faisait dire « je ne suis pas encore dans le groupe ». |
| **Rappel proactif une heure avant un événement** | 21 sept. 12:00 : « ⏰ Open Hour with @Gift starts in 1 hour — at 15:00 CAT / 14:00 WAT / 16:00 EAT » | **Nouveau (opt-in)** : activité « A word before each session », à partir des échéances horodatées, une fois par événement, dans les groupes non silencieux. |
| **Réponses dans n'importe quelle langue** (sesotho compris) | 21 sept. 11:44–11:48 : questions en sesotho, réponses en sesotho | **Nouveau** : la langue détectée par le modèle est gardée quelle qu'elle soit (code + nom), la réponse est écrite dedans. |
| **Précision factuelle, qui/quand** | « The official deadline was Thursday 17 September, but late declarations are still accepted — just confirm with @Diane » | Oui, avec en plus la **source vérifiée** (réponse au message ou citation), que meti_ai n'affiche pas. |
| **Réponse à « quel bot est actif ? »**, conscience de la situation du groupe | 20 sept. 04:32 | Oui via le brief communautaire (autres bots listés) et l'état de Jeli. |
| **Supprime ses mauvaises réponses** | nombreux « Ce message a été supprimé » | Oui, sur correction d'un membre (« c'est faux »). |
| **Message d'au revoir / passage en DM** | 20 sept. 08:47 | Jeli répond en DM ; pas de message d'au revoir automatique (choix anti-ban). |
| **Ton** : poli, structuré, emojis sobres, listes à puces courtes en français | 21 sept. 11:22 | Persona unique, plus court et plus oral ; vocal humain (voir §3). |

## 2. Ce que meti_ai fait moins bien, où Jeli garde l'avantage

- **Pas de sources** : meti_ai affirme, Jeli montre le message d'origine (réponse au message, ou citation avec l'auteur et le jour) et garde le reste pour « source ? ».
- **Beaucoup de messages supprimés** (au moins 15 sur 181) : des réponses envoyées puis retirées, y compris à des questions adressées à Diane. Jeli ne répond qu'aux messages qui lui sont adressés, plus les « déjà répondu » plafonnés.
- **Répond à des questions adressées aux organisateurs** (« @Diane approved this ? » → « ☝️ ») : Jeli ignore un message qui mentionne ou répond à quelqu'un d'autre.
- **Pas de vocal, pas d'images, pas de documents traduits, pas de récap de session enregistrée** (transcription à la minute avec lien) : toutes fonctions de Jeli.
- **Rappels seulement génériques** : Jeli fait aussi les rappels à la demande d'un membre (« rappelle-moi avant la réunion »).

## 3. Ce qui a été amélioré chez Jeli ce 22 septembre

1. **Rotation des modèles par santé mesurée** (`app/answer/llm.py`) — c'était la panne : la production ne tournait que sur **4 clés** (le journal ne cite que Key 0 à 3 : à vérifier dans la variable `GEMINI_API_KEY` sur Railway, une liste séparée par des virgules), `gemini-3.6-flash` était à son quota du jour, et les modèles Lite de Google étaient surchargés (503) ou muets (timeout de 25 s mesuré depuis ce poste) alors que `gemini-3.1-flash-lite` répondait en 2–3 s et `gemini-3-flash-preview` en 4–5 s. Avec un ordre fixe, chaque réponse payait 6 s d'échec avant d'atteindre le modèle qui marchait : médiane 16,5 s, p90 28,6 s, 11 réponses « sources seules » en 12 h. Désormais : un modèle qui échoue lentement se met en retrait pour toutes les clés (1, 2, 4… minutes, max 15), les modèles qui répondent passent devant, le type d'erreur et la durée sont journalisés, le nombre de clés est écrit au démarrage et visible sur `/health` avec la santé de chaque modèle. Les modèles qui refusent le « thinking » (`gemini-3-flash-preview`, 3.7, 3.8) sont appelés sans.
2. **Modèles ajoutés** : `gemini-3-flash-preview` (réponses, puis léger) ; ordre des Lite revu (`gemini-3.1-flash-lite` d'abord).
3. **« Pas dans le groupe »** : l'état de Jeli dit explicitement qu'une période calme n'est pas une déconnexion.
4. **Voix plus humaine et plus émotive** : sept humeurs au lieu de trois (joyeuse, excitée, taquine, rassurante, empathique, sérieuse, calme), une **indication de jeu** écrite par le modèle pour chaque vocal (rythme, pauses, mots appuyés, sourire) transmise aux voix Gemini, une consigne de diction commune (« une personne qui enregistre un vocal pour un ami, pas un lecteur »), et une réécriture orale qui tutoie, utilise le prénom et se permet de petites réactions.
5. **Toutes les langues** : réponse écrite dans la langue du membre même hors de la liste (sesotho, haoussa, portugais…).
6. **Rappel avant les sessions** (opt-in, page Activities).

## 4. Ce qui reste pour être n°1 sans discussion

- Vérifier les clés sur Railway (15 attendues, 4 vues) : c'est ce qui limite le plus Jeli aujourd'hui.
- Activer « A word before each session » et le digest quotidien dans le grand groupe le jour où les organisateurs l'y autorisent.
- Étiqueter les groupes en direct sur la page Knowledge (les citations disent encore « the group »).
- Ajouter `message.reaction` aux événements WAHA pour compter les 👍/👎.
