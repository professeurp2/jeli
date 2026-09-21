# Plan d'amélioration de Jeli — d'un moteur de recherche à un assistant humain

**Date :** lundi 21 septembre 2026 (J-3 avant la soumission du jeudi 24 ; créneaux de test des juges réservés jusqu'au 3 octobre).
**Périmètre :** l'agent conversationnel (compréhension, mémoire, recherche, réponse, ton, canal WhatsApp). Le dashboard, l'infra et l'anti-ban ne sont touchés que là où ils conditionnent la qualité des réponses.

---

## 0. Ce que j'ai vérifié pour écrire ce plan

| Source | Ce que j'en ai tiré |
|---|---|
| Code de `main` (e6676ef) + les 4 branches non fusionnées (`claude/renseignement-projet-2facb5`, `feat/dashboard-images`, `fix/catchup-liste-plate`, `fix/vocal-strip-sans-module`) | Tout ce que contiennent ces branches est déjà dans `main` sous une autre forme ; il n'y a rien à récupérer dedans. |
| Les 4 sessions précédentes avec d'autres modèles (~250 messages de toi, 19 PR, ~50 commits en 48 h) | Les retours des testeurs et tes demandes récurrentes : « trop robotique », « perd le contexte », « références aléatoires », « n'est pas précis », « fuite de mémoire », « doit poser des questions pour préciser », « doit réagir émotionnellement », « présentation trop technique ». |
| `README.md`, `Docs/hackathon_spec.md`, `Docs/hackathon_brief.md`, le PDF des organisateurs | Le critère n°2 du cahier des charges est **« réponses ancrées, chaque réponse cite d'où elle vient »** ; le critère n°1 est « ça tourne vraiment ». |
| Base de production Supabase (lecture seule, 21 sept. 21 h) | Chiffres ci-dessous. |
| Liste des modèles Gemini disponibles sur la clé | Les modèles d'image codés dans `illustrator.py` n'existent pas (404 dans les logs du 21 sept.). Voir §1.3. |
| `python -m pytest` | CI verte sur `main`. En local, `edge_tts` manque dans le venv (`pip install -r requirements-dev.txt`). |

### 0.1 Chiffres de production (7 derniers jours)

| Indicateur | Valeur | Lecture |
|---|---|---|
| Questions sur WhatsApp | 77 répondues · 44 « je ne sais pas » · 3 « sources seules » | **35 % de non-réponses**. Une partie est légitime (« Qui est le président du Mali ? »), une bonne partie ne l'est pas (voir §1.1). |
| Latence des questions | médiane 2,9 s · **p90 21,9 s** · max 95 s | L'objectif du cahier des charges est < 10 s. Une question sur dix est très lente. |
| Base de connaissances | 1 015 chunks · 989 caractères et 5 messages par chunk en moyenne | Taille raisonnable, mais polluée (ligne suivante). |
| Doublons | **611 messages présents dans plusieurs chats** (même auteur, même heure, même texte) et **945 lignes en double dans un même chat** | L'historique du groupe principal a été importé 3 fois sous 3 identifiants (`meti-cohort-2026`, `from18-20-september`, `meti-cohort-before-jeli-joined`) en plus du direct (`120363429618850959@g.us`). Chaque doublon occupe une place parmi les 6 extraits donnés au modèle. |
| Auteur le plus présent | le bot concurrent `+229 01 49 48 62 56` : 353 messages | Exclu à l'indexation depuis le 21 sept., mais les chunks indexés avant le contiennent toujours. « Nexus Bot » (57 messages) n'apparaît pas nommément dans `ignored_authors` : à vérifier par son numéro. |
| Réglages dashboard | `enabled.deadlines = False` | **L'extraction des échéances est désactivée** : `/deadlines` et le « à venir » des récaps ne se mettent plus à jour. Probablement coupé pour économiser le quota, sans alerte. |
| Réglages dashboard | groupe principal en mode silencieux, 7 groupes silencieux au total | Normal en phase de test (les organisateurs exigent des créneaux), mais cela veut dire que Jeli n'a jamais encore répondu « en conditions réelles » dans le grand groupe. |

### 0.2 Exemples de questions réelles qui ont mal tourné (dashboard, 20–21 sept.)

| Question posée | Résultat | Ce qu'un humain aurait fait |
|---|---|---|
| « session 4 » (après que Jeli a listé les sessions 1–4) | je ne sais pas | Comprendre le « 4 » grâce à son propre message précédent. Corrigé depuis par une regex spéciale, mais le problème de fond (pas de mémoire de ce que Jeli vient de dire) reste. |
| « en vocal » | je ne sais pas | Comprendre « réponds en vocal à ce qu'on vient de dire ». |
| « Thank you Jeli » / « jeli » | traités comme des questions, réponse RAG | Un « avec plaisir » ou un « oui ? ». |
| « How can you currently help a person who is visually impaired ? » | je ne sais pas | Parler de ses vocaux : c'est une question sur Jeli, pas sur le groupe. |
| « sends me some sort of summary daily. Is that part of what it was instructed ? » | je ne sais pas | Dire que ce n'est pas lui (c'est un autre bot) et expliquer son digest opt-in. |
| « Fais-moi un recap d'aujourd'hui » | recap de session au lieu du récap de la journée | Une seule compréhension de « recap » selon le contexte. |
| « Can you speak Afrikaan language ? » | réponse citant « Peace and blessings, family ! » de Stanley | Pas de source pour une question sur ses capacités. C'est ce cas qui a fait supprimer toutes les sources. |

---

## 1. Diagnostic : pourquoi Jeli paraît « pas intelligent » et « robotique »

Les forces d'abord, pour ne pas les casser : l'ingestion (exports, direct, enregistrements YouTube/Drive transcrits à la minute, documents page par page, sondages), l'hybride pgvector + mots-clés, le repli multi-modèles et multi-clés, le suivi de conversation 5–15 min, les réponses « jamais un simple je ne sais pas » (`awareness.py`), le rythme humain (vu → écrit… → envoi), les vocaux dans les deux sens, le dashboard de contrôle, les 130 tests. C'est une base solide et rare pour 4 jours de travail.

Les causes racines, avec les preuves :

### 1.1 Le « cerveau » est une cascade de regex, pas une compréhension

`app/answer/responder.py:126-183` décide dans cet ordre : `/help` → regex catch-up → regex échéances → regex recap → numéro nu → `/search` → puis seulement `Understander` (qui n'est appelé au modèle **que** si le message n'est pas une « question claire », `understand.py:125-128`) → fichier → catch-up → échéances → session entière → RAG. Chaque bug remonté ces deux jours a été corrigé en ajoutant une regex (`BARE_SESSION_NUMBER`, `VAGUE_SIGNAL`, `IMAGE_REQUEST` de 30 lignes, `VOICE_ASK`, `CATCHUP_PHRASE`…). Résultat : le comportement dépend de la formulation exacte, pas du sens, et le modèle n'a jamais la vue d'ensemble de ce que Jeli sait faire (lister ses documents, lire une session entière, résumer une période, envoyer un fichier, illustrer, parler).

### 1.2 Jeli n'a pas de mémoire, il a un cache

- Conversation : 4 tours / 20 min **en RAM** (`conversation.py:20-21,55`). Il y a eu une quinzaine de déploiements par jour ces derniers jours : à chaque redéploiement, toutes les conversations en cours sont perdues. C'est la « fuite de mémoire » constatée en comparant Jeli aux autres bots.
- Membre : rien. Jeli ne sait pas à qui il parle (prénom, langue préférée, projet, ce qu'il lui a déjà dit hier). Le seul état par membre est `_seen_members` (pour la voix), lui aussi en RAM.
- Culture générale du groupe : rien. Chaque question repart d'une recherche vectorielle, y compris « c'est quoi le programme ? », « qui est Diane ? », « quels programmes en parallèle ? ». Les 6 mails d'histoire du programme ont été mis en documents, mais ils ne sont trouvés que si la recherche les remonte. D'où « quel programme ? » dans la présentation.

### 1.3 La recherche est correcte mais bruyante

- Les chunks embarquent les horodatages bruts (`chunker.py:32` : `[2026-09-12 14:05 UTC] Auteur : texte`) : du bruit dans l'embedding, et rien qui dise *de quoi parle* le chunk.
- Le plein-texte est `to_tsvector('simple')` (`db/schema.sql`) : pas de racines (« échéance/échéances »), pas d'accents, et la requête est un **OU** de tous les mots (`search.py:21-24`) : « module » remonte tous les chunks des enregistrements.
- Aucune prise en compte de la récence, alors que le spec la demandait (« semantic + recency ») et que les questions des membres sont surtout « la dernière annonce », « le dernier message de Diane », « c'est quand ».
- 12 candidats → 6 extraits, dont une part de doublons (§0.1) et d'anciens chunks de bots.
- Les modèles d'image codés (`illustrator.py:23-27` : `gemini-2.0-flash-preview-image-generation`, `-exp`) n'existent pas sur la clé. Disponibles aujourd'hui : `gemini-3.1-flash-image`, `gemini-3.1-flash-lite-image`, `gemini-2.5-flash-image`, `gemini-3-pro-image`. TTS disponibles : `gemini-3.1-flash-tts-preview`, `gemini-2.5-flash-preview-tts` (quota gratuit vu à 10 requêtes/jour). Embedding : `gemini-embedding-2` existe (réindexation complète si on y passe).

### 1.4 Les sources ont été supprimées au lieu d'être réparées

`QUOTES_SHOWN = 0` (`rag.py:56`). La cause des « références aléatoires » est mécanique : l'attribution se fait **par recouvrement de mots** entre la réponse et les messages du chunk (`rag.py:135-137`), et une source est jointe même quand la question porte sur Jeli lui-même. Or « chaque réponse cite d'où elle vient » est le critère n°2 des juges, et c'est ce qui distingue un assistant fiable d'un bot qui affirme. Il faut une attribution vérifiée et discrète, pas zéro attribution.

### 1.5 Six voix différentes

Six prompts système coexistent (`prompts.SYSTEM`, `awareness.SYSTEM`, `catchup.SYSTEM`, `recaps.SYSTEM` et `SESSION_QUESTION_SYSTEM`, `understand.SYSTEM`, `DUPLICATE_SYSTEM`) avec des consignes de ton différentes, plus une trentaine de textes fixes bilingues (`language.py`) pour les cas « sociaux ». Le membre passe d'un Jeli « collègue vif » (RAG) à un Jeli « formulaire » (échéances, recap avec en-têtes 📝/✅/⏱️) à un Jeli « texte canné » (salutations). C'est la sensation « robot ».

### 1.6 La langue est devinée par listes de mots

`detect_language()` compte des mots dans 6 listes et est recalculée **trois fois** par message (`responder.py:94`, `rag.py:222`, `whatsapp_waha.py:751`). Sur un message court ou mixte (« ok merci Jeli, and the deadline ? ») elle se trompe, et les listes swahili/kinyarwanda (« na », « ni », « mu ») produisent des faux positifs.

### 1.7 La latence explose sur les mauvais jours

`llm._build_pairs()` (`llm.py:87-105`) met les paires (clé, modèle) en repos **en fin de liste au lieu de les exclure** : avec 7 clés × 3 modèles et un timeout de 6 s, une réponse peut essayer 21 paires. Ajouté aux 2 à 3 appels modèle par message (compréhension, réponse, explication du « je ne sais pas »), et aux timeouts de 25–30 s des chemins session/catch-up, on obtient le p90 à 22 s et le max à 95 s.

### 1.8 Jeli ne voit pas tout ce que les humains voient

Les images postées dans le groupe (affiches, captures d'écran de planning) ne sont décrites que si le message est adressé à Jeli (`whatsapp_waha.py:617`) ; les liens ne sont pas lus (sauf les enregistrements) ; les vocaux du groupe ne sont pas écoutés. Tu l'avais demandé le 19 sept. (« tout doit être sauvegardé de façon structurée »), ce n'est fait que pour les documents et les sondages.

### 1.9 Les réactions émoji sont aveugles

`emotion_emoji()` réagit par regex à **tous** les messages de groupe non adressés à Jeli (`whatsapp_waha.py:625-630`) : chaque « merci », « bonjour », 🔥 déclenche une réaction. Sur 244 membres, c'est du bruit, un motif de signalement (anti-ban), et ce n'est pas de l'émotion : c'est un réflexe.

### 1.10 La boucle de qualité est cassée

- `scripts/evaluate.py:35-37` cherche « 📌 » dans la réponse pour la juger « ancrée » : depuis la suppression des sources, **toute question attendue répondue est FAIL**. Plus aucune mesure objective depuis le 21 sept.
- Le jeu d'évaluation fait 18 questions, sans conversation multi-tours, sans cas « social », sans ton, sans langue.
- Aucun retour des membres n'est capté (👍/👎, « c'est faux ») : `is_correction()` efface le message mais n'enregistre rien.
- L'extraction des échéances est coupée sans que rien ne le signale.

---

## 2. La cible : ce que veut dire « assistant humain » pour Jeli

Un bon collègue qui a une mémoire parfaite du groupe fait cinq choses. Chaque chantier ci-dessous sert au moins l'une d'elles.

1. **Il comprend** ce qu'on lui demande, y compris à demi-mot, et demande une précision quand il hésite (une question courte, avec des options).
2. **Il se souvient** : de la conversation en cours, de la personne, et de la vie du groupe (les programmes, les gens, les dates).
3. **Il sait où chercher** : dans les échanges, dans une session précise, dans un document, dans la période demandée, ou il sait que ce n'est nulle part.
4. **Il répond juste et dit d'où il le tient**, avec la longueur qu'il faut, et n'invente jamais.
5. **Il parle comme une personne du groupe** : sa langue, son registre, la mise en forme WhatsApp, une voix quand ça s'y prête, une image quand ça aide, et jamais deux fois la même formule.

---

## 3. Les chantiers

Effort en jours-personne, pour une personne qui connaît le code. Impact : ce que le membre ressent.

### C1 — Hygiène de la mémoire (0,5 j · impact fort · risque faible) — À FAIRE EN PREMIER

Objectif : que les 6 extraits donnés au modèle soient 6 extraits utiles.

1. **Dédoublonner** : nouveau `scripts/dedupe.py` qui (a) remappe les alias `meti-cohort-2026`, `from18-20-september`, `meti-cohort-before-jeli-joined` vers l'identifiant réel `120363429618850959@g.us` (les citations « répondre au message » et les libellés deviennent cohérents), (b) supprime les messages identiques (auteur + heure + texte) en gardant la version live quand elle existe, (c) rechunke et ré-embarque tout (≈ 335 k tokens : quelques minutes avec 7 clés et le `TokenBudget` existant). Dry-run d'abord, avec compte avant/après.
2. **Sortir les bots des anciens chunks** : la réindexation ci-dessus le fait, à condition que `ignored_authors` contienne aussi « Nexus Bot » (nom **et** numéro : `is_ignored` compare les deux, `citations.py:68-74`).
3. **Réactiver l'extraction des échéances** (`enabled.deadlines`) avec une limite de lots par heure si c'était une question de quota, et afficher sur l'Overview un avertissement quand une activité de base est coupée.
4. **Corriger les modèles** : images → `gemini-3.1-flash-image` puis `gemini-2.5-flash-image` (v1beta, clés gratuites) ; TTS → `gemini-3.1-flash-tts-preview` puis `gemini-2.5-flash-preview-tts` puis edge-tts ; un test au démarrage qui liste les modèles et logue ceux qui manquent, pour ne plus découvrir un 404 en production.
5. `pip install -r requirements-dev.txt` en local pour que la suite de tests tourne avant chaque push (elle ne tourne aujourd'hui que dans la CI).

### C2 — Fiabilité et latence (0,5 j · impact fort · risque faible)

Objectif : p90 sous 8 s, jamais de réponse à 95 s.

1. `_build_pairs()` : n'inclure les paires en repos **que** s'il n'en reste aucune de fraîche ; plafonner à 4 tentatives par appel ; un budget global par message (`asyncio.timeout(8)`) après lequel Jeli envoie le repli « c'est chargé, voilà où le groupe en a parlé » plutôt que d'attendre.
2. Lancer la recherche vectorielle **en parallèle** de l'appel de compréhension (sur le texte brut), et ne relancer qu'avec les requêtes réécrites si elles diffèrent.
3. Cache des réponses (question normalisée + chat, 10 min) comme pour les catch-ups : un jury qui pose la même question en rafale coûte un appel.
4. Sur le dashboard : quota restant estimé par clé et par modèle (compte des 429 de la dernière heure), pour voir venir la panne.

### C3 — Un seul cerveau : l'agent avec outils (1,5 j · impact très fort · risque moyen) — LE CŒUR DU PLAN

Objectif : remplacer la cascade de regex par **une** compréhension qui connaît tout ce que Jeli sait faire.

Principe : un appel Gemini avec *function calling* (`thinking_level="minimal"`, `temperature 0.2`), un maximum de 3 tours d'outils, puis une réponse structurée. Le modèle voit : la persona (C7), la fiche du groupe (C4), l'état de Jeli (`awareness.state()` : sessions transcrites/en cours, documents, mémoire jusqu'à quand), le profil du membre (C4), la conversation persistée (C4), et le message (avec le message cité). Les outils :

```
search_memory(query, chat=None, since=None, until=None, kind=None) -> extraits numérotés par message
read_session(session, question)        -> réponse depuis la transcription entière + moments
list_sessions() / list_documents()     -> ce que Jeli a (pour « combien de PDF as-tu ? »)
catchup(since, until, chat=None)       -> digest de la période (cache 10 min)
upcoming_deadlines(days)               -> les échéances
send_document(document, language=None) -> le fichier, traduit si demandé
where_discussed(topic)                 -> /search (sans modèle)
```

La réponse finale porte : `answer`, `sources` (identifiants de messages, vérifiés par C6), `language`, `wants_voice`, `image_topic` (vide sauf demande ou statistiques), `clarify_options` (quand il vaut mieux demander). `responder.py` devient un répartiteur mince : commandes `/`, salutations et remerciements en chemin rapide (regex, zéro appel), garde-fous, puis l'agent. Toutes les regex d'intention actuelles deviennent des **tests** du comportement de l'agent, pas du code de production.

Ce que ça règle d'un coup : « 4 » après une liste, « recap d'aujourd'hui » vs recap de session, « en vocal », « combien de documents ? », « qu'a dit Diane en dernier ? » (recherche avec `since` et tri par date), « et pour la vidéo ? », les questions sur Jeli lui-même, les questions vagues (le modèle choisit de demander).

Budget quota : en moyenne 1 appel + 1 outil par question (contre 2 à 3 appels aujourd'hui), grâce aux chemins rapides et au cache. Les modèles lite ne suivent pas toujours les outils : garder `gemini-3.6-flash` puis `gemini-3.5-flash` pour l'agent, et n'utiliser les lite que pour les tâches sans outils (traduction, transcription, digests).

**Variante minimale si le temps manque (0,5 j) :** garder l'architecture actuelle mais faire de `Understander` la *seule* porte d'entrée (appelée pour tout message hors chemins rapides), avec tous les genres d'intention (`question`, `catchup{since,until}`, `recap{session}`, `session_question{session}`, `deadlines`, `file{document,language}`, `list{documents|sessions}`, `about_jeli`, `social`, `clarify{options}`, `voice`, `image{topic}`) et `language`. C'est 60 % du bénéfice pour un tiers de l'effort ; l'agent complet peut venir pendant la fenêtre de test.

### C4 — Une mémoire à trois niveaux (1 j · impact très fort · risque faible)

1. **Conversation, persistée** : table `jeli.conversations (chat_id, member_key, at, role, text, meta jsonb)`. Fenêtre de 30 min ou 8 tours, incluant *ce que Jeli a dit* (sa liste de sessions, sa question de clarification en attente, l'image proposée). Survit aux redéploiements. Pour les messages privés : conservés 24 h seulement, jamais indexés, et dit dans la section Vie privée du README.
2. **Le membre** : table `jeli.members (member_key, name, language, prefers_voice, project, country, first_seen, last_seen, notes jsonb)`, remplie automatiquement (présentation « je m'appelle… je travaille sur… », langue des messages, réactions à la voix) et par le membre (« appelle-moi Awa », « parle-moi en anglais »). Injectée dans le prompt : « Awa (Mali), projet X, préfère le français, t'a demandé hier les échéances Wadhwani ». Sans numéro de téléphone dans le prompt ; `/oublie-moi` efface tout ; visible et effaçable sur le dashboard.
3. **La fiche du groupe (culture générale)** : table `jeli.facts (fact, programme, valid_from, valid_to, source_message_id, confidence)` alimentée par une activité horaire qui lit les nouvelles annonces des organisateurs, les documents et les recaps, et maintient 40 à 80 faits stables (les programmes et leurs rôles, les organisateurs, les dates clés, les règles du hackathon, les sessions passées et prévues, les autres bots en test). Rendue en ≤ 1 500 tokens en tête de **tous** les prompts. Jeli sait enfin « quel programme », qui est Diane, et que le digest quotidien est celui d'un autre bot, sans recherche.

### C5 — Une recherche qui trouve le bon extrait (1 j · impact fort · risque moyen)

1. **Chunks contextualisés** : le texte embarqué commence par une en-tête lisible (« Discussion dans METI cohort, jeudi 17 sept. 2026 vers 14 h, avec Diane (organisatrice), Awa, Moussa. Sujet : … ») et les lignes perdent l'horodatage brut (il reste en métadonnées). Le « Sujet : » d'une ligne est écrit par un modèle lite à l'indexation (1 000 appels une fois, puis quelques dizaines par jour) : c'est la « contextual retrieval » qui fait le plus gagner sur des messages courts.
2. **Plein-texte multilingue** : colonne `search` = `to_tsvector('french', unaccent(content)) || to_tsvector('english', unaccent(content))` ; requête où les entités (majuscules, nombres, dates, noms de sessions) sont **obligatoires** (`&`) et les autres mots optionnels (`|`).
3. **Récence** : dans la fusion, un bonus décroissant avec l'âge (demi-vie 30 jours) et, pour les questions datées (« dernier », « cette semaine », « c'est quand »), un filtre `since` posé par l'agent.
4. **Plus de candidats, mieux triés** : 24 fusionnés → suppression des quasi-doublons (chunks partageant > 50 % de messages) → 10 extraits, chaque **message** numéré (`[3.2]`), pour que le modèle cite au message et non au chunk.
5. **Le seuil** : garder 0,60 mais le tester sur les 3 meilleurs, et laisser l'agent décider « rien dans le groupe » avec la fiche (C4) sous les yeux : on ne répond plus « je ne sais pas » à « c'est quoi Wadhwani ».
6. Après le hackathon : passer à `gemini-embedding-2` (réindexation) et mesurer sur le jeu C9 avant de garder.

### C6 — Des sources fiables et discrètes (0,5 j · impact fort · risque faible)

Rétablir les sources en corrigeant ce qui les rendait fausses :

1. Le modèle cite des **messages** (`[3.2]`), pas des chunks ; chaque citation est vérifiée (au moins deux mots pleins, un nombre ou une date en commun avec la phrase de la réponse), sinon elle tombe.
2. **Une seule source**, la mieux vérifiée, et seulement pour une réponse factuelle (date, règle, décision, « qui a dit ») : jamais pour une salutation, une question sur Jeli, un catch-up. Présentée comme sur WhatsApp : *répondre au message* quand il a été dit en direct dans ce chat, sinon une ligne `> Diane · organisatrice · jeu. 17 sept.` avec la phrase exacte.
3. « Source ? » / « d'où tu tiens ça ? » donne toutes les sources de la dernière réponse (mémoire C4).
4. Interrupteur sur le dashboard (off / une source / sur demande), réglé sur « une source » pour la démo aux juges.

### C7 — Une seule voix, humaine (0,5 j · impact fort · risque faible)

1. Un `app/answer/persona.py` unique, importé par tous les prompts : Jeli, assistant griot de la cohorte ; chaleureux, direct, drôle avec parcimonie ; **la langue et le registre du membre** ; son prénom quand on le connaît ; longueur proportionnelle à la question (une ligne pour un oui/non, jamais plus de 6 lignes sans qu'on demande une liste) ; mise en forme WhatsApp (*gras* pour les dates et noms, pas d'en-têtes de section sauf pour un vrai récap) ; jamais deux fois la même formule d'ouverture ou de fermeture ; jamais « je suis la mémoire du groupe ».
2. **Questions de clarification à options** : quand l'agent hésite entre deux sessions, deux programmes, deux périodes, il pose une question d'une ligne avec 2 ou 3 choix (« Tu parles de la session MIT du 16 ou du coaching Wadhwani du 17 ? ») et la garde en mémoire : « la deuxième » suffit ensuite. Plus de phrase cannée `vague`.
3. **La langue par le modèle** (champ `language` de l'agent), calculée une fois, transmise partout ; les listes de mots ne servent plus qu'aux textes système hors ligne.
4. **Réactions dosées** : réagir seulement (a) aux messages adressés à Jeli, (b) aux annonces des organisateurs, (c) aux messages où le modèle a détecté une émotion forte dans le cadre de C3 — au plus 10 réactions par heure et par groupe, jamais sur un simple « merci ». Les stickers : une réaction seulement en conversation avec Jeli.
5. **Deux bulles quand c'est naturel** : une réponse courte puis, 2–3 s après, la précision ou la source, comme une personne qui complète. Optionnel, à mesurer sur le ressenti.
6. Voix : garder « vocal pour vocal », premier contact et demande explicite ; le texte lu par la TTS est déjà nettoyé (`for_speech`) ; ajouter au prompt TTS l'émotion voulue par l'agent (« enthousiaste », « rassurant »).

### C8 — Jeli voit tout ce que le groupe voit (0,5 à 1 j · impact moyen · risque quota)

1. **Images du groupe** : décrire chaque image postée (Gemini vision, 1 appel, modèle lite) et l'enregistrer comme message « [Image partagée par Diane : affiche… « Open Hour jeudi 15 h CAT »] » : les affiches et captures de planning deviennent cherchables. Limite : 60 images/jour, organisateurs d'abord.
2. **Liens** : pour les pages publiques (Google Docs publiés, formulaires, articles), garder le titre et les 500 premiers caractères comme message associé. Les enregistrements suivent déjà leur propre chemin.
3. **Vocaux des organisateurs** dans le groupe : transcrits et enregistrés (les vocaux des membres restent privés, comme aujourd'hui).
4. Chaque élément garde auteur, heure, chat, et un type (`image`, `link`, `voice`) pour que C6 le cite correctement (« sur l'affiche partagée par Diane le 16 »).

### C9 — La boucle de qualité (1 j · impact durable · risque nul)

C'est ce qui permet d'améliorer « comme un assistant humain » **après** le 24, pendant que les juges testent.

1. **Réparer `scripts/evaluate.py`** : juger « répondu » avec `Reply.unanswered`/l'issue, pas avec « 📌 » ; vérifier `expect_any` sur le texte ; vérifier la source attendue via les identifiants de messages (C6).
2. **Trois jeux d'évaluation** dans `evals/` :
   - `retrieval.json` : 40 questions → identifiants de messages attendus, tirées des vraies questions du dashboard et des exports ; métrique recall@6 et @10.
   - `answers.json` : 40 questions avec réponse de référence ; **juge LLM** (`gemini-3.5-flash`, clé dédiée) sur une grille en 5 points : exactitude, ancrage, langue, longueur, ton.
   - `conversations.json` : 15 scénarios multi-tours (suivi, clarification, « 4 », « en vocal », message cité, correction) joués contre `Responder` avec un canal factice.
3. **Nightly** (activité planifiée, 1 clé réservée) : score publié sur l'Overview ; la CI ne joue que 10 cas sans modèle (mocks) pour rester rapide.
4. **Retour des membres** : abonner WAHA à `message.reaction` ; un 👍/👎/❤️ sur un message de Jeli, et un `is_correction()`, écrivent dans `jeli.feedback (message_id, question, answer, verdict)`. La page Questions montre « à revoir » et un bouton « ajouter au jeu d'éval ». C'est la source des 40 questions de référence, renouvelée chaque semaine.

### C10 — Proactivité mesurée (0,5 j · impact moyen · risque anti-ban)

Rien de nouveau qui parte de Jeli sans sollicitation (règle anti-ban conservée). En revanche :

1. Nouveau membre qui se présente **en s'adressant à Jeli** ou en DM : accueil personnalisé (prénom, pays, lien vers le doc d'info) et enregistrement du profil (C4).
2. Dans chaque catch-up et digest : « à faire avant demain » en tête quand une échéance tombe sous 24 h (extraction réactivée, C1).
3. R7 (« le groupe a déjà répondu ») : rester à 3/h/groupe, mais avec la fiche (C4) le modèle confirme mieux.
4. L'offre d'illustration existante (« tu veux une image ? ») reste limitée aux statistiques, comme décidé.

---

## 4. Calendrier proposé

| Quand | Quoi | Livrable visible |
|---|---|---|
| **Lun 21 soir → mar 22 midi** | C1 (hygiène + modèles), C2 (latence), C9.1 (réparer l'éval), C7.1 (persona unique) | Base propre et réindexée ; p90 < 8 s ; éval qui remesure ; un seul ton. |
| **Mar 22 après-midi → mer 23** | C3 (agent, ou la variante minimale si mercredi midi ce n'est pas stable), C4 (mémoire persistée + profil + fiche), C6 (une source vérifiée), C7.2–7.4 (clarifications, langue, réactions) | « 4 », « en vocal », « recap d'aujourd'hui », « combien de documents », « qui est Diane » répondent juste ; les conversations survivent aux déploiements ; une source discrète et exacte. |
| **Jeu 24 (matin)** | Gel. C9.2 sur 20 cas, README (sections « comment Jeli comprend », « ce que Jeli retient », « vie privée » mises à jour), script de démo, soumission. | Soumis avec de la marge. |
| **25 sept → 3 oct (fenêtre de test des juges)** | C5 (recherche : contexte, FTS, récence), C8 (images/liens), C9.3–9.4 (nightly, feedback 👍/👎), C10, puis itérations quotidiennes sur les questions « à revoir ». | Score nightly qui monte ; liste « je ne sais pas » qui baisse. |

Ordre de priorité si le temps manque : C1 → C2 → C9.1 → C3 (variante minimale) → C4.1 (conversation persistée) → C4.3 (fiche) → C6 → C7 → C4.2 → C5 → C8 → C10.

---

## 5. Comment on saura que ça marche

| Mesure | Aujourd'hui | Cible 24 sept. | Cible 3 oct. |
|---|---|---|---|
| Non-réponses sur questions *dans le périmètre* (WhatsApp) | ~35 % toutes questions confondues | < 20 % | < 10 % |
| Latence p90 des questions | 21,9 s | < 8 s | < 6 s |
| recall@6 sur `evals/retrieval.json` | non mesuré | > 75 % | > 85 % |
| Note du juge LLM (exactitude · ancrage · ton), sur 5 | non mesuré | ≥ 3,8 | ≥ 4,3 |
| Citations hors sujet (revue manuelle de 30 réponses) | sources supprimées | 0 sur 30 | 0 sur 30 |
| Conversations perdues après déploiement | toutes | 0 | 0 |
| 👎 / (👍 + 👎) sur les messages de Jeli | non capté | capté | < 15 % |

---

## 6. Risques et garde-fous

- **Quota Gemini gratuit** : c'est la contrainte n°1. Chaque chantier réduit ou plafonne les appels (chemins rapides, cache, un agent au lieu de trois appels, lite pour l'indexation). Garder une clé réservée aux évaluations et une au TTS. Décider avant la démo si un palier payant de secours est acceptable pour le jour J.
- **Anti-ban** : rien ne change au rythme d'envoi ; C7.4 *réduit* le volume de réactions ; C10 n'ajoute aucun message non sollicité.
- **Régressions** : chaque regex retirée devient un test ; C9.1 avant C3 pour mesurer avant/après ; déployer C3 derrière un interrupteur dashboard (`brain: agent | classic`) pour revenir en arrière en un clic.
- **Vie privée** : profil membre sans numéro, effaçable, documenté ; DM conservés 24 h pour le fil, jamais indexés ; images décrites uniquement dans les groupes autorisés.
- **Temps** : la variante minimale de C3 existe précisément pour ne pas arriver jeudi avec un agent à moitié fait.

---

## 7. Décisions à prendre (par toi et l'équipe)

1. **Agent avec outils (C3) ou variante minimale ?** Ma recommandation : lancer l'agent mardi, avec la variante minimale comme filet si mercredi midi il n'est pas stable.
2. **Rétablir une source vérifiée (C6) ?** Ma recommandation : oui, une seule, discrète, désactivable ; c'est le critère n°2 des juges et la seule preuve que Jeli n'invente pas.
3. **Mémoire par membre (C4.2)** : d'accord pour retenir prénom, langue, projet et l'historique récent, avec `/oublie-moi` et une ligne dans le README ?
4. **Descriptions d'images du groupe (C8.1)** : d'accord pour envoyer les images du groupe à Gemini (elles restent dans Supabase, région UE) ?
5. **Embedding v2** : après le 24 seulement, sur mesure.

---

## 8. Réalisé le 21 septembre au soir (commit `fc03396`, branche `claude/chatbot-rag-improvement-plan-49cb10`)

| Chantier | Fait | Reste |
|---|---|---|
| C1 Hygiène | `scripts/hygiene.py` exécuté en production : 3 alias fusionnés en `meti-cohort-2026`, **945 doublons supprimés** (3 517 → 2 572 messages), « Nexus Bot » (nom + identifiant) ajouté aux auteurs ignorés, extraction des échéances réactivée, chunks supprimés et réindexés par le serveur. Modèles image (`gemini-3.1-flash-image`, `gemini-2.5-flash-image`) et TTS (`gemini-3.1-flash-tts-preview`) corrigés ; vérification des modèles au démarrage. | Relancer `python -m scripts.hygiene --apply --keep-deadlines-off` **après le déploiement** pour que les chunks prennent le nouveau format (en-tête lisible). |
| C2 Latence | Repli borné à 4 tentatives (meilleur modèle sur 2 clés, puis les suivants), paires en repos exclues, cache des réponses 10 min, recherche lancée en parallèle de la compréhension. | Quota par clé sur le dashboard. |
| C3 Cerveau | Compréhension v2 : une seule porte d'entrée (`understand.py`) avec toutes les intentions, la langue, la période, la session, les options de clarification ; regex conservées en repli hors modèle. | L'agent à outils complet (function calling) reste pour la fenêtre de test. |
| C4 Mémoire | Conversations persistées (`jeli.conversations`, 30 min, 8 tours), profil membre (`jeli.members`), brief communautaire (`jeli.briefs`, activité toutes les 6 h, injecté dans tous les prompts). | Réglage « effacer un membre » sur le dashboard. |
| C5 Recherche | En-têtes de chunk lisibles sans horodatage brut, plein-texte français + anglais, bonus de récence, 16 candidats → 8 extraits, quasi-doublons fusionnés. | Résumé « Sujet : » par chunk ; `gemini-embedding-2` après le 24. |
| C6 Sources | Citation au message `[n.m]`, vérification par recouvrement (mot, nombre ou date), une source affichée sous les réponses factuelles, « source ? » pour le reste, réglage dashboard (une / sur demande / jamais). | — |
| C7 Voix | Persona unique dans les 6 prompts, questions de clarification à options, langue donnée par le modèle, réactions limitées aux vraies émotions (10/h/groupe). | Deux bulles ; émotion transmise à la TTS. |
| C8 Ingestion | Images postées dans les groupes décrites et mémorisées (60/jour). | Liens ; vocaux des organisateurs. |
| C9 Qualité | `scripts/evaluate.py` réparé, 41 cas dont 18 de compréhension ; retours 👍/👎 et corrections enregistrés (`jeli.feedback`, événement `message.reaction` à activer côté WAHA). | Juge LLM, exécution nocturne, page dashboard. |
| C10 Proactivité | Inchangé (règle anti-ban). | Accueil personnalisé des nouveaux en DM. |

Migration de schéma appliquée en production (tables `conversations`, `briefs`, `members`, `feedback`, colonne `search` FR+EN). 216 tests passent. À faire par l'équipe : fusionner et pousser la branche (CI → Railway), ajouter `message.reaction` à `WHATSAPP_HOOK_EVENTS` sur le service WAHA, puis relancer l'hygiène pour le nouveau format de chunks.
