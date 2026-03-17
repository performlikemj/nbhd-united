"""Localized error messages for Telegram/LINE error paths.

These messages are shown when the tenant's AI assistant is unavailable
(budget exhausted, provisioning, suspended). Because the assistant is
unreachable in these states, we use pre-translated static strings rather
than runtime LLM calls.

Follows the same MESSAGES dict + helper pattern as onboarding.py.
"""

# ---------------------------------------------------------------------------
# Error message templates
# ---------------------------------------------------------------------------
# Placeholders:
#   budget_exhausted:    {remaining}, {plus_message}, {billing_url}
#   waking_up:           (none)
#   hibernation_waking:  (none)
#   suspended:           {billing_url}
# ---------------------------------------------------------------------------

ERROR_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "budget_exhausted": (
            "You've hit your monthly quota."
            " ${remaining} remaining."
            " New messages are blocked until the next monthly reset."
            "{plus_message}"
            " Open Billing to upgrade/manage at {billing_url}."
        ),
        "waking_up": (
            "Your assistant is waking up! \U0001f305"
            " This usually takes about a minute."
            " Just send your message again in a moment!"
        ),
        "hibernation_waking": (
            "Your assistant is waking up from a break! \u2600\ufe0f"
            " Your message has been received and will be delivered shortly."
            " This usually takes about a minute."
        ),
        "suspended": (
            "Your assistant is paused."
            " Running an AI agent costs real money"
            " \u2014 cloud servers, model tokens (every reply costs us), and storage."
            " We keep things transparent so you know exactly where your money goes.\n\n"
            "Ready to pick up where you left off? {billing_url}"
        ),
    },
    "ja": {
        "budget_exhausted": (
            "\u6708\u9593\u4e0a\u9650\u306b\u9054\u3057\u307e\u3057\u305f\u3002"
            "\u6b8b\u308a${remaining}\u3067\u3059\u3002"
            "\u6b21\u306e\u6708\u6b21\u30ea\u30bb\u30c3\u30c8\u307e\u3067\u65b0\u3057\u3044\u30e1\u30c3\u30bb\u30fc\u30b8\u306f\u30d6\u30ed\u30c3\u30af\u3055\u308c\u307e\u3059\u3002"
            "{plus_message}"
            "\u8acb\u6c42\u30da\u30fc\u30b8\u3067\u30a2\u30c3\u30d7\u30b0\u30ec\u30fc\u30c9/\u7ba1\u7406: {billing_url}"
        ),
        "waking_up": (
            "\u30a2\u30b7\u30b9\u30bf\u30f3\u30c8\u304c\u8d77\u52d5\u4e2d\u3067\u3059\uff01\U0001f305"
            "\u901a\u5e38\u7d041\u5206\u307b\u3069\u304b\u304b\u308a\u307e\u3059\u3002"
            "\u5c11\u3057\u5f85\u3063\u3066\u304b\u3089\u3082\u3046\u4e00\u5ea6\u30e1\u30c3\u30bb\u30fc\u30b8\u3092\u9001\u3063\u3066\u304f\u3060\u3055\u3044\uff01"
        ),
        "hibernation_waking": (
            "\u30a2\u30b7\u30b9\u30bf\u30f3\u30c8\u304c\u4f11\u61a9\u304b\u3089\u8d77\u52d5\u4e2d\u3067\u3059\uff01\u2600\ufe0f"
            "\u30e1\u30c3\u30bb\u30fc\u30b8\u3092\u53d7\u3051\u53d6\u308a\u307e\u3057\u305f\u3002\u307e\u3082\u306a\u304f\u304a\u5c4a\u3051\u3057\u307e\u3059\u3002"
            "\u901a\u5e38\u7d041\u5206\u307b\u3069\u304b\u304b\u308a\u307e\u3059\u3002"
        ),
        "suspended": (
            "\u30a2\u30b7\u30b9\u30bf\u30f3\u30c8\u306f\u4e00\u6642\u505c\u6b62\u4e2d\u3067\u3059\u3002"
            "AI\u30a8\u30fc\u30b8\u30a7\u30f3\u30c8\u306e\u904b\u7528\u306b\u306f\u5b9f\u969b\u306e\u8cbb\u7528\u304c\u304b\u304b\u308a\u307e\u3059"
            "\u2014\u30af\u30e9\u30a6\u30c9\u30b5\u30fc\u30d0\u30fc\u3001\u30e2\u30c7\u30eb\u30c8\u30fc\u30af\u30f3\u3001\u30b9\u30c8\u30ec\u30fc\u30b8\u3002"
            "\u900f\u660e\u6027\u3092\u5927\u5207\u306b\u3057\u3066\u3044\u307e\u3059\u3002\n\n"
            "\u518d\u958b\u3057\u307e\u3059\u304b\uff1f {billing_url}"
        ),
    },
    "es": {
        "budget_exhausted": (
            "Has alcanzado tu cuota mensual."
            " Quedan ${remaining}."
            " Los mensajes nuevos est\u00e1n bloqueados hasta el pr\u00f3ximo reinicio mensual."
            "{plus_message}"
            " Abre Facturaci\u00f3n para actualizar/gestionar: {billing_url}"
        ),
        "waking_up": (
            "\u00a1Tu asistente se est\u00e1 despertando! \U0001f305"
            " Normalmente tarda alrededor de un minuto."
            " \u00a1Env\u00eda tu mensaje de nuevo en un momento!"
        ),
        "hibernation_waking": (
            "\u00a1Tu asistente est\u00e1 volviendo de un descanso! \u2600\ufe0f"
            " Tu mensaje ha sido recibido y ser\u00e1 entregado en breve."
            " Normalmente tarda alrededor de un minuto."
        ),
        "suspended": (
            "Tu asistente est\u00e1 en pausa."
            " Ejecutar un agente de IA cuesta dinero real"
            " \u2014 servidores en la nube, tokens de modelo y almacenamiento."
            " Mantenemos todo transparente para que sepas a d\u00f3nde va tu dinero.\n\n"
            "\u00bfListo para retomar? {billing_url}"
        ),
    },
    "fr": {
        "budget_exhausted": (
            "Vous avez atteint votre quota mensuel."
            " ${remaining} restant."
            " Les nouveaux messages sont bloqu\u00e9s jusqu\u2019au prochain renouvellement mensuel."
            "{plus_message}"
            " Ouvrez la facturation pour g\u00e9rer votre abonnement\u00a0: {billing_url}"
        ),
        "waking_up": (
            "Votre assistant se r\u00e9veille\u00a0! \U0001f305"
            " Cela prend g\u00e9n\u00e9ralement environ une minute."
            " Renvoyez votre message dans un instant\u00a0!"
        ),
        "hibernation_waking": (
            "Votre assistant revient de pause\u00a0! \u2600\ufe0f"
            " Votre message a \u00e9t\u00e9 re\u00e7u et sera livr\u00e9 sous peu."
            " Cela prend g\u00e9n\u00e9ralement environ une minute."
        ),
        "suspended": (
            "Votre assistant est en pause."
            " Faire fonctionner un agent IA co\u00fbte de l\u2019argent r\u00e9el"
            " \u2014 serveurs cloud, tokens de mod\u00e8le et stockage."
            " Nous restons transparents pour que vous sachiez o\u00f9 va votre argent.\n\n"
            "Pr\u00eat \u00e0 reprendre\u00a0? {billing_url}"
        ),
    },
    "de": {
        "budget_exhausted": (
            "Du hast dein monatliches Kontingent erreicht."
            " ${remaining} \u00fcbrig."
            " Neue Nachrichten sind bis zum n\u00e4chsten monatlichen Reset blockiert."
            "{plus_message}"
            " \u00d6ffne die Abrechnung zum Upgraden/Verwalten: {billing_url}"
        ),
        "waking_up": (
            "Dein Assistent wird gerade gestartet! \U0001f305"
            " Das dauert normalerweise etwa eine Minute."
            " Sende deine Nachricht gleich noch einmal!"
        ),
        "hibernation_waking": (
            "Dein Assistent kommt gerade aus einer Pause zur\u00fcck! \u2600\ufe0f"
            " Deine Nachricht wurde empfangen und wird in K\u00fcrze zugestellt."
            " Das dauert normalerweise etwa eine Minute."
        ),
        "suspended": (
            "Dein Assistent ist pausiert."
            " Ein KI-Agent zu betreiben kostet echtes Geld"
            " \u2014 Cloud-Server, Modell-Token und Speicher."
            " Wir sind transparent, damit du wei\u00dft, wohin dein Geld geht.\n\n"
            "Bereit weiterzumachen? {billing_url}"
        ),
    },
    "pt": {
        "budget_exhausted": (
            "Voc\u00ea atingiu sua cota mensal."
            " ${remaining} restante."
            " Novas mensagens est\u00e3o bloqueadas at\u00e9 a pr\u00f3xima renova\u00e7\u00e3o mensal."
            "{plus_message}"
            " Abra o Faturamento para upgrade/gerenciamento: {billing_url}"
        ),
        "waking_up": (
            "Seu assistente est\u00e1 iniciando! \U0001f305"
            " Isso geralmente leva cerca de um minuto."
            " Envie sua mensagem novamente em instantes!"
        ),
        "hibernation_waking": (
            "Seu assistente est\u00e1 voltando de um intervalo! \u2600\ufe0f"
            " Sua mensagem foi recebida e ser\u00e1 entregue em breve."
            " Isso geralmente leva cerca de um minuto."
        ),
        "suspended": (
            "Seu assistente est\u00e1 pausado."
            " Executar um agente de IA custa dinheiro real"
            " \u2014 servidores na nuvem, tokens de modelo e armazenamento."
            " Mantemos tudo transparente para que voc\u00ea saiba para onde vai seu dinheiro.\n\n"
            "Pronto para continuar? {billing_url}"
        ),
    },
    "zh": {
        "budget_exhausted": (
            "\u60a8\u5df2\u8fbe\u5230\u6708\u5ea6\u914d\u989d\u3002"
            "\u5269\u4f59${remaining}\u3002"
            "\u5728\u4e0b\u6b21\u6708\u5ea6\u91cd\u7f6e\u4e4b\u524d\uff0c\u65b0\u6d88\u606f\u5c06\u88ab\u62e6\u622a\u3002"
            "{plus_message}"
            "\u6253\u5f00\u8ba1\u8d39\u9875\u9762\u5347\u7ea7/\u7ba1\u7406: {billing_url}"
        ),
        "waking_up": (
            "\u60a8\u7684\u52a9\u624b\u6b63\u5728\u542f\u52a8\uff01\U0001f305"
            "\u901a\u5e38\u9700\u8981\u7ea6\u4e00\u5206\u949f\u3002"
            "\u8bf7\u7a0d\u540e\u518d\u53d1\u9001\u6d88\u606f\uff01"
        ),
        "hibernation_waking": (
            "\u60a8\u7684\u52a9\u624b\u6b63\u5728\u4ece\u4f11\u606f\u4e2d\u5524\u9192\uff01\u2600\ufe0f"
            "\u60a8\u7684\u6d88\u606f\u5df2\u6536\u5230\uff0c\u5c06\u5f88\u5feb\u9001\u8fbe\u3002"
            "\u901a\u5e38\u9700\u8981\u7ea6\u4e00\u5206\u949f\u3002"
        ),
        "suspended": (
            "\u60a8\u7684\u52a9\u624b\u5df2\u6682\u505c\u3002"
            "\u8fd0\u884cAI\u4ee3\u7406\u9700\u8981\u5b9e\u9645\u8d39\u7528"
            "\u2014\u2014\u4e91\u670d\u52a1\u5668\u3001\u6a21\u578b\u4ee4\u724c\u548c\u5b58\u50a8\u3002"
            "\u6211\u4eec\u4fdd\u6301\u900f\u660e\uff0c\u8ba9\u60a8\u77e5\u9053\u94b1\u82b1\u5728\u4e86\u54ea\u91cc\u3002\n\n"
            "\u51c6\u5907\u7ee7\u7eed\u4e86\u5417\uff1f{billing_url}"
        ),
    },
    "ko": {
        "budget_exhausted": (
            "\uc6d4\uac04 \ud560\ub2f9\ub7c9\uc5d0 \ub3c4\ub2ec\ud588\uc2b5\ub2c8\ub2e4."
            " \ub0a8\uc740 \uae08\uc561: ${remaining}."
            " \ub2e4\uc74c \uc6d4\ubcc4 \ucd08\uae30\ud654\uae4c\uc9c0 \uc0c8 \uba54\uc2dc\uc9c0\uac00 \ucc28\ub2e8\ub429\ub2c8\ub2e4."
            "{plus_message}"
            " \uc5c5\uadf8\ub808\uc774\ub4dc/\uad00\ub9ac\ud558\ub824\uba74 \uccad\uad6c \ud398\uc774\uc9c0\ub97c \uc5ec\uc138\uc694: {billing_url}"
        ),
        "waking_up": (
            "\uc5b4\uc2dc\uc2a4\ud134\ud2b8\uac00 \uc2dc\uc791\ub418\uace0 \uc788\uc2b5\ub2c8\ub2e4! \U0001f305"
            " \ubcf4\ud1b5 \uc57d 1\ubd84 \uc815\ub3c4 \uac78\ub9bd\ub2c8\ub2e4."
            " \uc7a0\uc2dc \ud6c4 \ub2e4\uc2dc \uba54\uc2dc\uc9c0\ub97c \ubcf4\ub0b4\uc8fc\uc138\uc694!"
        ),
        "hibernation_waking": (
            "\uc5b4\uc2dc\uc2a4\ud134\ud2b8\uac00 \ud734\uc2dd\uc5d0\uc11c \ub3cc\uc544\uc654\uc2b5\ub2c8\ub2e4! \u2600\ufe0f"
            " \uba54\uc2dc\uc9c0\uac00 \uc811\uc218\ub418\uc5c8\uc73c\uba70 \uacf3 \uc804\ub2ec\ub420 \uc608\uc815\uc785\ub2c8\ub2e4."
            " \ubcf4\ud1b5 \uc57d 1\ubd84 \uc815\ub3c4 \uac78\ub9bd\ub2c8\ub2e4."
        ),
        "suspended": (
            "\uc5b4\uc2dc\uc2a4\ud134\ud2b8\uac00 \uc77c\uc2dc \uc815\uc9c0\ub418\uc5c8\uc2b5\ub2c8\ub2e4."
            " AI \uc5d0\uc774\uc804\ud2b8\ub97c \uc6b4\uc601\ud558\ub824\uba74 \uc2e4\uc81c \ube44\uc6a9\uc774 \ub4ed\ub2c8\ub2e4"
            " \u2014 \ud074\ub77c\uc6b0\ub4dc \uc11c\ubc84, \ubaa8\ub378 \ud1a0\ud070, \uc800\uc7a5\uc18c."
            " \ud22c\uba85\ud558\uac8c \uc6b4\uc601\ud558\uc5ec \ube44\uc6a9\uc774 \uc5b4\ub514\uc5d0 \uc4f0\uc774\ub294\uc9c0 \uc54c \uc218 \uc788\uc2b5\ub2c8\ub2e4.\n\n"
            "\ub2e4\uc2dc \uc2dc\uc791\ud560 \uc900\ube44\uac00 \ub418\uc168\ub098\uc694? {billing_url}"
        ),
    },
    "it": {
        "budget_exhausted": (
            "Hai raggiunto la tua quota mensile."
            " ${remaining} rimanenti."
            " I nuovi messaggi sono bloccati fino al prossimo rinnovo mensile."
            "{plus_message}"
            " Apri la fatturazione per aggiornare/gestire: {billing_url}"
        ),
        "waking_up": (
            "Il tuo assistente si sta avviando! \U0001f305"
            " Di solito ci vuole circa un minuto."
            " Invia di nuovo il tuo messaggio tra un momento!"
        ),
        "hibernation_waking": (
            "Il tuo assistente sta tornando dalla pausa! \u2600\ufe0f"
            " Il tuo messaggio \u00e8 stato ricevuto e verr\u00e0 consegnato a breve."
            " Di solito ci vuole circa un minuto."
        ),
        "suspended": (
            "Il tuo assistente \u00e8 in pausa."
            " Eseguire un agente AI costa denaro reale"
            " \u2014 server cloud, token del modello e archiviazione."
            " Manteniamo tutto trasparente perch\u00e9 tu sappia dove vanno i tuoi soldi.\n\n"
            "Pronto a riprendere? {billing_url}"
        ),
    },
    "nl": {
        "budget_exhausted": (
            "Je hebt je maandelijkse limiet bereikt."
            " ${remaining} over."
            " Nieuwe berichten zijn geblokkeerd tot de volgende maandelijkse reset."
            "{plus_message}"
            " Open Facturering om te upgraden/beheren: {billing_url}"
        ),
        "waking_up": (
            "Je assistent wordt opgestart! \U0001f305"
            " Dit duurt meestal ongeveer een minuut."
            " Stuur je bericht zo meteen opnieuw!"
        ),
        "hibernation_waking": (
            "Je assistent komt terug van een pauze! \u2600\ufe0f"
            " Je bericht is ontvangen en wordt binnenkort afgeleverd."
            " Dit duurt meestal ongeveer een minuut."
        ),
        "suspended": (
            "Je assistent is gepauzeerd."
            " Het draaien van een AI-agent kost echt geld"
            " \u2014 cloudservers, modeltokens en opslag."
            " We zijn transparant zodat je weet waar je geld naartoe gaat.\n\n"
            "Klaar om verder te gaan? {billing_url}"
        ),
    },
    "ru": {
        "budget_exhausted": (
            "\u0412\u044b \u0438\u0441\u0447\u0435\u0440\u043f\u0430\u043b\u0438 \u043c\u0435\u0441\u044f\u0447\u043d\u0443\u044e \u043a\u0432\u043e\u0442\u0443."
            " \u041e\u0441\u0442\u0430\u0442\u043e\u043a: ${remaining}."
            " \u041d\u043e\u0432\u044b\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u044b \u0434\u043e \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0435\u0433\u043e \u0435\u0436\u0435\u043c\u0435\u0441\u044f\u0447\u043d\u043e\u0433\u043e \u0441\u0431\u0440\u043e\u0441\u0430."
            "{plus_message}"
            " \u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 \u0440\u0430\u0437\u0434\u0435\u043b \u043e\u043f\u043b\u0430\u0442\u044b \u0434\u043b\u044f \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f: {billing_url}"
        ),
        "waking_up": (
            "\u0412\u0430\u0448 \u0430\u0441\u0441\u0438\u0441\u0442\u0435\u043d\u0442 \u0437\u0430\u043f\u0443\u0441\u043a\u0430\u0435\u0442\u0441\u044f! \U0001f305"
            " \u041e\u0431\u044b\u0447\u043d\u043e \u044d\u0442\u043e \u0437\u0430\u043d\u0438\u043c\u0430\u0435\u0442 \u043e\u043a\u043e\u043b\u043e \u043c\u0438\u043d\u0443\u0442\u044b."
            " \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0441\u043d\u043e\u0432\u0430 \u0447\u0435\u0440\u0435\u0437 \u043c\u0438\u043d\u0443\u0442\u043a\u0443!"
        ),
        "hibernation_waking": (
            "\u0412\u0430\u0448 \u0430\u0441\u0441\u0438\u0441\u0442\u0435\u043d\u0442 \u0432\u043e\u0437\u0432\u0440\u0430\u0449\u0430\u0435\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u043f\u0435\u0440\u0435\u0440\u044b\u0432\u0430! \u2600\ufe0f"
            " \u0412\u0430\u0448\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u043e \u0438 \u0431\u0443\u0434\u0435\u0442 \u0434\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043e \u0432 \u0431\u043b\u0438\u0436\u0430\u0439\u0448\u0435\u0435 \u0432\u0440\u0435\u043c\u044f."
            " \u041e\u0431\u044b\u0447\u043d\u043e \u044d\u0442\u043e \u0437\u0430\u043d\u0438\u043c\u0430\u0435\u0442 \u043e\u043a\u043e\u043b\u043e \u043c\u0438\u043d\u0443\u0442\u044b."
        ),
        "suspended": (
            "\u0412\u0430\u0448 \u0430\u0441\u0441\u0438\u0441\u0442\u0435\u043d\u0442 \u043f\u0440\u0438\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d."
            " \u0420\u0430\u0431\u043e\u0442\u0430 AI-\u0430\u0433\u0435\u043d\u0442\u0430 \u0441\u0442\u043e\u0438\u0442 \u0440\u0435\u0430\u043b\u044c\u043d\u044b\u0445 \u0434\u0435\u043d\u0435\u0433"
            " \u2014 \u043e\u0431\u043b\u0430\u0447\u043d\u044b\u0435 \u0441\u0435\u0440\u0432\u0435\u0440\u044b, \u0442\u043e\u043a\u0435\u043d\u044b \u043c\u043e\u0434\u0435\u043b\u0438 \u0438 \u0445\u0440\u0430\u043d\u0438\u043b\u0438\u0449\u0435."
            " \u041c\u044b \u0441\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u043c \u043f\u0440\u043e\u0437\u0440\u0430\u0447\u043d\u043e\u0441\u0442\u044c, \u0447\u0442\u043e\u0431\u044b \u0432\u044b \u0437\u043d\u0430\u043b\u0438, \u043a\u0443\u0434\u0430 \u0438\u0434\u0443\u0442 \u0432\u0430\u0448\u0438 \u0434\u0435\u043d\u044c\u0433\u0438.\n\n"
            "\u0413\u043e\u0442\u043e\u0432\u044b \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c? {billing_url}"
        ),
    },
    "ar": {
        "budget_exhausted": (
            "\u0644\u0642\u062f \u0648\u0635\u0644\u062a \u0625\u0644\u0649 \u0627\u0644\u062d\u062f \u0627\u0644\u0634\u0647\u0631\u064a."
            " \u0645\u062a\u0628\u0642\u064a ${remaining}."
            " \u0627\u0644\u0631\u0633\u0627\u0626\u0644 \u0627\u0644\u062c\u062f\u064a\u062f\u0629 \u0645\u062d\u0638\u0648\u0631\u0629 \u062d\u062a\u0649 \u0625\u0639\u0627\u062f\u0629 \u0627\u0644\u0636\u0628\u0637 \u0627\u0644\u0634\u0647\u0631\u064a\u0629 \u0627\u0644\u0642\u0627\u062f\u0645\u0629."
            "{plus_message}"
            " \u0627\u0641\u062a\u062d \u0635\u0641\u062d\u0629 \u0627\u0644\u0641\u0648\u0627\u062a\u064a\u0631 \u0644\u0644\u062a\u0631\u0642\u064a\u0629/\u0627\u0644\u0625\u062f\u0627\u0631\u0629: {billing_url}"
        ),
        "waking_up": (
            "\u0645\u0633\u0627\u0639\u062f\u0643 \u064a\u0633\u062a\u064a\u0642\u0638! \U0001f305"
            " \u0639\u0627\u062f\u0629\u064b \u064a\u0633\u062a\u063a\u0631\u0642 \u0647\u0630\u0627 \u062d\u0648\u0627\u0644\u064a \u062f\u0642\u064a\u0642\u0629."
            " \u0623\u0631\u0633\u0644 \u0631\u0633\u0627\u0644\u062a\u0643 \u0645\u0631\u0629 \u0623\u062e\u0631\u0649 \u0628\u0639\u062f \u0644\u062d\u0638\u0627\u062a!"
        ),
        "hibernation_waking": (
            "\u0645\u0633\u0627\u0639\u062f\u0643 \u064a\u0639\u0648\u062f \u0645\u0646 \u0627\u0633\u062a\u0631\u0627\u062d\u0629! \u2600\ufe0f"
            " \u062a\u0645 \u0627\u0633\u062a\u0644\u0627\u0645 \u0631\u0633\u0627\u0644\u062a\u0643 \u0648\u0633\u064a\u062a\u0645 \u062a\u0633\u0644\u064a\u0645\u0647\u0627 \u0642\u0631\u064a\u0628\u064b\u0627."
            " \u0639\u0627\u062f\u0629\u064b \u064a\u0633\u062a\u063a\u0631\u0642 \u0647\u0630\u0627 \u062d\u0648\u0627\u0644\u064a \u062f\u0642\u064a\u0642\u0629."
        ),
        "suspended": (
            "\u0645\u0633\u0627\u0639\u062f\u0643 \u0645\u062a\u0648\u0642\u0641 \u0645\u0624\u0642\u062a\u064b\u0627."
            " \u062a\u0634\u063a\u064a\u0644 \u0648\u0643\u064a\u0644 \u0630\u0643\u0627\u0621 \u0627\u0635\u0637\u0646\u0627\u0639\u064a \u064a\u0643\u0644\u0641 \u0623\u0645\u0648\u0627\u0644\u064b\u0627 \u062d\u0642\u064a\u0642\u064a\u0629"
            " \u2014 \u062e\u0648\u0627\u062f\u0645 \u0633\u062d\u0627\u0628\u064a\u0629\u060c \u0631\u0645\u0648\u0632 \u0627\u0644\u0646\u0645\u0648\u0630\u062c\u060c \u0648\u062a\u062e\u0632\u064a\u0646."
            " \u0646\u062d\u0631\u0635 \u0639\u0644\u0649 \u0627\u0644\u0634\u0641\u0627\u0641\u064a\u0629 \u062d\u062a\u0649 \u062a\u0639\u0631\u0641 \u0623\u064a\u0646 \u062a\u0630\u0647\u0628 \u0623\u0645\u0648\u0627\u0644\u0643.\n\n"
            "\u0645\u0633\u062a\u0639\u062f \u0644\u0644\u0645\u062a\u0627\u0628\u0639\u0629\u061f {billing_url}"
        ),
    },
    "hi": {
        "budget_exhausted": (
            "\u0906\u092a\u0915\u093e \u092e\u093e\u0938\u093f\u0915 \u0915\u094b\u091f\u093e \u092a\u0942\u0930\u093e \u0939\u094b \u0917\u092f\u093e \u0939\u0948\u0964"
            " ${remaining} \u0936\u0947\u0937\u0964"
            " \u0905\u0917\u0932\u0947 \u092e\u093e\u0938\u093f\u0915 \u0930\u093f\u0938\u0947\u091f \u0924\u0915 \u0928\u090f \u0938\u0902\u0926\u0947\u0936 \u092c\u094d\u0932\u0949\u0915 \u0939\u0948\u0902\u0964"
            "{plus_message}"
            " \u0905\u092a\u0917\u094d\u0930\u0947\u0921/\u092a\u094d\u0930\u092c\u0902\u0927\u0928 \u0915\u0947 \u0932\u093f\u090f \u092c\u093f\u0932\u093f\u0902\u0917 \u0916\u094b\u0932\u0947\u0902: {billing_url}"
        ),
        "waking_up": (
            "\u0906\u092a\u0915\u093e \u0938\u0939\u093e\u092f\u0915 \u0936\u0941\u0930\u0942 \u0939\u094b \u0930\u0939\u093e \u0939\u0948! \U0001f305"
            " \u0906\u092e\u0924\u094c\u0930 \u092a\u0930 \u092f\u0939 \u0932\u0917\u092d\u0917 \u090f\u0915 \u092e\u093f\u0928\u091f \u0932\u0947\u0924\u093e \u0939\u0948\u0964"
            " \u0915\u0943\u092a\u092f\u093e \u0925\u094b\u0921\u093c\u0940 \u0926\u0947\u0930 \u092e\u0947\u0902 \u0926\u094b\u092c\u093e\u0930\u093e \u0938\u0902\u0926\u0947\u0936 \u092d\u0947\u091c\u0947\u0902!"
        ),
        "hibernation_waking": (
            "\u0906\u092a\u0915\u093e \u0938\u0939\u093e\u092f\u0915 \u0935\u093f\u0930\u093e\u092e \u0938\u0947 \u0932\u094c\u091f \u0930\u0939\u093e \u0939\u0948! \u2600\ufe0f"
            " \u0906\u092a\u0915\u093e \u0938\u0902\u0926\u0947\u0936 \u092a\u094d\u0930\u093e\u092a\u094d\u0924 \u0939\u094b \u0917\u092f\u093e \u0939\u0948 \u0914\u0930 \u091c\u0932\u094d\u0926 \u0939\u0940 \u092a\u0939\u0941\u0901\u091a\u093e \u0926\u093f\u092f\u093e \u091c\u093e\u090f\u0917\u093e\u0964"
            " \u0906\u092e\u0924\u094c\u0930 \u092a\u0930 \u092f\u0939 \u0932\u0917\u092d\u0917 \u090f\u0915 \u092e\u093f\u0928\u091f \u0932\u0947\u0924\u093e \u0939\u0948\u0964"
        ),
        "suspended": (
            "\u0906\u092a\u0915\u093e \u0938\u0939\u093e\u092f\u0915 \u0930\u0941\u0915\u093e \u0939\u0941\u0906 \u0939\u0948\u0964"
            " AI \u090f\u091c\u0947\u0902\u091f \u091a\u0932\u093e\u0928\u0947 \u092e\u0947\u0902 \u0905\u0938\u0932\u0940 \u092a\u0948\u0938\u0947 \u0932\u0917\u0924\u0947 \u0939\u0948\u0902"
            " \u2014 \u0915\u094d\u0932\u093e\u0909\u0921 \u0938\u0930\u094d\u0935\u0930, \u092e\u0949\u0921\u0932 \u091f\u094b\u0915\u0928 \u0914\u0930 \u0938\u094d\u091f\u094b\u0930\u0947\u091c\u0964"
            " \u0939\u092e \u092a\u093e\u0930\u0926\u0930\u094d\u0936\u093f\u0924\u093e \u092c\u0928\u093e\u090f \u0930\u0916\u0924\u0947 \u0939\u0948\u0902 \u0924\u093e\u0915\u093f \u0906\u092a \u091c\u093e\u0928 \u0938\u0915\u0947\u0902 \u0915\u093f \u0906\u092a\u0915\u093e \u092a\u0948\u0938\u093e \u0915\u0939\u093e\u0901 \u091c\u093e\u0924\u093e \u0939\u0948\u0964\n\n"
            "\u092b\u093f\u0930 \u0938\u0947 \u0936\u0941\u0930\u0942 \u0915\u0930\u0928\u0947 \u0915\u0947 \u0932\u093f\u090f \u0924\u0948\u092f\u093e\u0930? {billing_url}"
        ),
    },
    "tr": {
        "budget_exhausted": (
            "Ayl\u0131k kotan\u0131za ula\u015ft\u0131n\u0131z."
            " ${remaining} kald\u0131."
            " Yeni mesajlar bir sonraki ayl\u0131k s\u0131f\u0131rlamaya kadar engellendi."
            "{plus_message}"
            " Y\u00fckseltme/y\u00f6netim i\u00e7in faturay\u0131 a\u00e7\u0131n: {billing_url}"
        ),
        "waking_up": (
            "Asistan\u0131n\u0131z ba\u015flat\u0131l\u0131yor! \U0001f305"
            " Genellikle yakla\u015f\u0131k bir dakika s\u00fcrer."
            " Biraz sonra mesaj\u0131n\u0131z\u0131 tekrar g\u00f6nderin!"
        ),
        "hibernation_waking": (
            "Asistan\u0131n\u0131z moladan d\u00f6n\u00fcyor! \u2600\ufe0f"
            " Mesaj\u0131n\u0131z al\u0131nd\u0131 ve k\u0131sa s\u00fcre i\u00e7inde iletilecek."
            " Genellikle yakla\u015f\u0131k bir dakika s\u00fcrer."
        ),
        "suspended": (
            "Asistan\u0131n\u0131z duraklatild\u0131."
            " Bir AI ajan\u0131 \u00e7al\u0131\u015ft\u0131rmak ger\u00e7ek paraya mal olur"
            " \u2014 bulut sunucular, model tokenlar\u0131 ve depolama."
            " Paran\u0131z\u0131n nereye gitti\u011fini bilmeniz i\u00e7in \u015feffaf\u0131z.\n\n"
            "Devam etmeye haz\u0131r m\u0131s\u0131n\u0131z? {billing_url}"
        ),
    },
    "th": {
        "budget_exhausted": (
            "\u0e04\u0e38\u0e13\u0e43\u0e0a\u0e49\u0e42\u0e04\u0e15\u0e49\u0e32\u0e23\u0e32\u0e22\u0e40\u0e14\u0e37\u0e2d\u0e19\u0e04\u0e23\u0e1a\u0e41\u0e25\u0e49\u0e27"
            " \u0e40\u0e2b\u0e25\u0e37\u0e2d ${remaining}"
            " \u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21\u0e43\u0e2b\u0e21\u0e48\u0e08\u0e30\u0e16\u0e39\u0e01\u0e1a\u0e25\u0e47\u0e2d\u0e01\u0e08\u0e19\u0e01\u0e27\u0e48\u0e32\u0e08\u0e30\u0e23\u0e35\u0e40\u0e0b\u0e47\u0e15\u0e43\u0e19\u0e40\u0e14\u0e37\u0e2d\u0e19\u0e16\u0e31\u0e14\u0e44\u0e1b"
            "{plus_message}"
            " \u0e40\u0e1b\u0e34\u0e14\u0e2b\u0e19\u0e49\u0e32\u0e01\u0e32\u0e23\u0e40\u0e23\u0e35\u0e22\u0e01\u0e40\u0e01\u0e47\u0e1a\u0e40\u0e07\u0e34\u0e19\u0e40\u0e1e\u0e37\u0e48\u0e2d\u0e2d\u0e31\u0e1b\u0e40\u0e01\u0e23\u0e14/\u0e08\u0e31\u0e14\u0e01\u0e32\u0e23: {billing_url}"
        ),
        "waking_up": (
            "\u0e1c\u0e39\u0e49\u0e0a\u0e48\u0e27\u0e22\u0e02\u0e2d\u0e07\u0e04\u0e38\u0e13\u0e01\u0e33\u0e25\u0e31\u0e07\u0e40\u0e23\u0e34\u0e48\u0e21\u0e17\u0e33\u0e07\u0e32\u0e19! \U0001f305"
            " \u0e42\u0e14\u0e22\u0e1b\u0e01\u0e15\u0e34\u0e08\u0e30\u0e43\u0e0a\u0e49\u0e40\u0e27\u0e25\u0e32\u0e1b\u0e23\u0e30\u0e21\u0e32\u0e13\u0e2b\u0e19\u0e36\u0e48\u0e07\u0e19\u0e32\u0e17\u0e35"
            " \u0e01\u0e23\u0e38\u0e13\u0e32\u0e2a\u0e48\u0e07\u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21\u0e2d\u0e35\u0e01\u0e04\u0e23\u0e31\u0e49\u0e07\u0e43\u0e19\u0e2d\u0e35\u0e01\u0e2a\u0e31\u0e01\u0e04\u0e23\u0e39\u0e48!"
        ),
        "hibernation_waking": (
            "\u0e1c\u0e39\u0e49\u0e0a\u0e48\u0e27\u0e22\u0e02\u0e2d\u0e07\u0e04\u0e38\u0e13\u0e01\u0e25\u0e31\u0e1a\u0e21\u0e32\u0e08\u0e32\u0e01\u0e01\u0e32\u0e23\u0e1e\u0e31\u0e01\u0e1c\u0e48\u0e2d\u0e19\u0e41\u0e25\u0e49\u0e27! \u2600\ufe0f"
            " \u0e02\u0e49\u0e2d\u0e04\u0e27\u0e32\u0e21\u0e02\u0e2d\u0e07\u0e04\u0e38\u0e13\u0e44\u0e14\u0e49\u0e23\u0e31\u0e1a\u0e41\u0e25\u0e49\u0e27\u0e41\u0e25\u0e30\u0e08\u0e30\u0e2a\u0e48\u0e07\u0e16\u0e36\u0e07\u0e43\u0e19\u0e44\u0e21\u0e48\u0e0a\u0e49\u0e32"
            " \u0e42\u0e14\u0e22\u0e1b\u0e01\u0e15\u0e34\u0e08\u0e30\u0e43\u0e0a\u0e49\u0e40\u0e27\u0e25\u0e32\u0e1b\u0e23\u0e30\u0e21\u0e32\u0e13\u0e2b\u0e19\u0e36\u0e48\u0e07\u0e19\u0e32\u0e17\u0e35"
        ),
        "suspended": (
            "\u0e1c\u0e39\u0e49\u0e0a\u0e48\u0e27\u0e22\u0e02\u0e2d\u0e07\u0e04\u0e38\u0e13\u0e16\u0e39\u0e01\u0e2b\u0e22\u0e38\u0e14\u0e0a\u0e31\u0e48\u0e27\u0e04\u0e23\u0e32\u0e27"
            " \u0e01\u0e32\u0e23\u0e43\u0e0a\u0e49\u0e07\u0e32\u0e19 AI agent \u0e21\u0e35\u0e04\u0e48\u0e32\u0e43\u0e0a\u0e49\u0e08\u0e48\u0e32\u0e22\u0e08\u0e23\u0e34\u0e07"
            " \u2014 \u0e40\u0e0b\u0e34\u0e23\u0e4c\u0e1f\u0e40\u0e27\u0e2d\u0e23\u0e4c\u0e04\u0e25\u0e32\u0e27\u0e14\u0e4c \u0e42\u0e17\u0e40\u0e04\u0e47\u0e19\u0e42\u0e21\u0e40\u0e14\u0e25 \u0e41\u0e25\u0e30\u0e1e\u0e37\u0e49\u0e19\u0e17\u0e35\u0e48\u0e08\u0e31\u0e14\u0e40\u0e01\u0e47\u0e1a"
            " \u0e40\u0e23\u0e32\u0e40\u0e1b\u0e34\u0e14\u0e40\u0e1c\u0e22\u0e40\u0e1e\u0e37\u0e48\u0e2d\u0e43\u0e2b\u0e49\u0e04\u0e38\u0e13\u0e23\u0e39\u0e49\u0e27\u0e48\u0e32\u0e40\u0e07\u0e34\u0e19\u0e02\u0e2d\u0e07\u0e04\u0e38\u0e13\u0e44\u0e1b\u0e17\u0e35\u0e48\u0e44\u0e2b\u0e19\n\n"
            "\u0e1e\u0e23\u0e49\u0e2d\u0e21\u0e17\u0e35\u0e48\u0e08\u0e30\u0e01\u0e25\u0e31\u0e1a\u0e21\u0e32\u0e41\u0e25\u0e49\u0e27\u0e2b\u0e23\u0e37\u0e2d\u0e22\u0e31\u0e07? {billing_url}"
        ),
    },
    "vi": {
        "budget_exhausted": (
            "B\u1ea1n \u0111\u00e3 \u0111\u1ea1t h\u1ea1n ng\u1ea1ch h\u00e0ng th\u00e1ng."
            " C\u00f2n l\u1ea1i ${remaining}."
            " Tin nh\u1eafn m\u1edbi s\u1ebd b\u1ecb ch\u1eb7n cho \u0111\u1ebfn l\u1ea7n \u0111\u1eb7t l\u1ea1i h\u00e0ng th\u00e1ng ti\u1ebfp theo."
            "{plus_message}"
            " M\u1edf trang Thanh to\u00e1n \u0111\u1ec3 n\u00e2ng c\u1ea5p/qu\u1ea3n l\u00fd: {billing_url}"
        ),
        "waking_up": (
            "Tr\u1ee3 l\u00fd c\u1ee7a b\u1ea1n \u0111ang kh\u1edfi \u0111\u1ed9ng! \U0001f305"
            " Th\u01b0\u1eddng m\u1ea5t kho\u1ea3ng m\u1ed9t ph\u00fat."
            " H\u00e3y g\u1eedi l\u1ea1i tin nh\u1eafn sau \u00edt ph\u00fat!"
        ),
        "hibernation_waking": (
            "Tr\u1ee3 l\u00fd c\u1ee7a b\u1ea1n \u0111ang tr\u1edf l\u1ea1i sau gi\u1edd ngh\u1ec9! \u2600\ufe0f"
            " Tin nh\u1eafn c\u1ee7a b\u1ea1n \u0111\u00e3 \u0111\u01b0\u1ee3c nh\u1eadn v\u00e0 s\u1ebd \u0111\u01b0\u1ee3c g\u1eedi s\u1edbm."
            " Th\u01b0\u1eddng m\u1ea5t kho\u1ea3ng m\u1ed9t ph\u00fat."
        ),
        "suspended": (
            "Tr\u1ee3 l\u00fd c\u1ee7a b\u1ea1n \u0111\u00e3 b\u1ecb t\u1ea1m d\u1eebng."
            " V\u1eadn h\u00e0nh m\u1ed9t AI agent t\u1ed1n chi ph\u00ed th\u1ef1c t\u1ebf"
            " \u2014 m\u00e1y ch\u1ee7 \u0111\u00e1m m\u00e2y, token m\u00f4 h\u00ecnh v\u00e0 l\u01b0u tr\u1eef."
            " Ch\u00fang t\u00f4i minh b\u1ea1ch \u0111\u1ec3 b\u1ea1n bi\u1ebft ti\u1ec1n c\u1ee7a m\u00ecnh \u0111i \u0111\u00e2u.\n\n"
            "S\u1eb5n s\u00e0ng ti\u1ebfp t\u1ee5c? {billing_url}"
        ),
    },
    "pl": {
        "budget_exhausted": (
            "Osi\u0105gni\u0119to miesi\u0119czny limit."
            " Pozosta\u0142o ${remaining}."
            " Nowe wiadomo\u015bci s\u0105 zablokowane do nast\u0119pnego miesi\u0119cznego resetu."
            "{plus_message}"
            " Otw\u00f3rz p\u0142atno\u015bci, aby zaktualizowa\u0107/zarz\u0105dza\u0107: {billing_url}"
        ),
        "waking_up": (
            "Tw\u00f3j asystent si\u0119 uruchamia! \U0001f305"
            " Zwykle zajmuje to oko\u0142o minuty."
            " Wy\u015blij wiadomo\u015b\u0107 ponownie za chwil\u0119!"
        ),
        "hibernation_waking": (
            "Tw\u00f3j asystent wraca po przerwie! \u2600\ufe0f"
            " Twoja wiadomo\u015b\u0107 zosta\u0142a odebrana i zostanie dostarczona wkr\u00f3tce."
            " Zwykle zajmuje to oko\u0142o minuty."
        ),
        "suspended": (
            "Tw\u00f3j asystent jest wstrzymany."
            " Uruchamianie agenta AI kosztuje prawdziwe pieni\u0105dze"
            " \u2014 serwery chmurowe, tokeny modelu i pami\u0119\u0107 masowa."
            " Jeste\u015bmy transparentni, aby\u015b wiedzia\u0142, na co id\u0105 Twoje pieni\u0105dze.\n\n"
            "Gotowy, \u017ceby kontynuowa\u0107? {billing_url}"
        ),
    },
    "id": {
        "budget_exhausted": (
            "Anda telah mencapai kuota bulanan."
            " Tersisa ${remaining}."
            " Pesan baru diblokir hingga reset bulanan berikutnya."
            "{plus_message}"
            " Buka Tagihan untuk upgrade/kelola: {billing_url}"
        ),
        "waking_up": (
            "Asisten Anda sedang memulai! \U0001f305"
            " Biasanya memakan waktu sekitar satu menit."
            " Kirim pesan Anda lagi sebentar lagi!"
        ),
        "hibernation_waking": (
            "Asisten Anda kembali dari istirahat! \u2600\ufe0f"
            " Pesan Anda telah diterima dan akan segera dikirim."
            " Biasanya memakan waktu sekitar satu menit."
        ),
        "suspended": (
            "Asisten Anda dijeda."
            " Menjalankan agen AI memerlukan biaya nyata"
            " \u2014 server cloud, token model, dan penyimpanan."
            " Kami transparan agar Anda tahu ke mana uang Anda pergi.\n\n"
            "Siap untuk melanjutkan? {billing_url}"
        ),
    },
    "ms": {
        "budget_exhausted": (
            "Anda telah mencapai kuota bulanan."
            " Baki ${remaining}."
            " Mesej baharu disekat sehingga set semula bulanan seterusnya."
            "{plus_message}"
            " Buka Bil untuk naik taraf/urus: {billing_url}"
        ),
        "waking_up": (
            "Pembantu anda sedang bermula! \U0001f305"
            " Biasanya mengambil masa kira-kira satu minit."
            " Hantar mesej anda semula sebentar lagi!"
        ),
        "hibernation_waking": (
            "Pembantu anda kembali dari rehat! \u2600\ufe0f"
            " Mesej anda telah diterima dan akan dihantar tidak lama lagi."
            " Biasanya mengambil masa kira-kira satu minit."
        ),
        "suspended": (
            "Pembantu anda dijeda."
            " Menjalankan ejen AI memerlukan wang sebenar"
            " \u2014 pelayan awan, token model dan storan."
            " Kami telus supaya anda tahu ke mana wang anda pergi.\n\n"
            "Bersedia untuk meneruskan? {billing_url}"
        ),
    },
    "tl": {
        "budget_exhausted": (
            "Naabot mo na ang buwanang quota."
            " ${remaining} ang natitira."
            " Naka-block ang mga bagong mensahe hanggang sa susunod na buwanang reset."
            "{plus_message}"
            " Buksan ang Billing para mag-upgrade/mag-manage: {billing_url}"
        ),
        "waking_up": (
            "Nagsisimula ang iyong assistant! \U0001f305"
            " Karaniwang tumatagal ng isang minuto."
            " Ipadala ulit ang iyong mensahe maya-maya!"
        ),
        "hibernation_waking": (
            "Bumabalik ang iyong assistant mula sa pahinga! \u2600\ufe0f"
            " Natanggap na ang iyong mensahe at ipapadala ito sa lalong madaling panahon."
            " Karaniwang tumatagal ng isang minuto."
        ),
        "suspended": (
            "Naka-pause ang iyong assistant."
            " Ang pagpapatakbo ng AI agent ay nagkakahalaga ng totoong pera"
            " \u2014 cloud servers, model tokens, at storage."
            " Transparent kami para malaman mo kung saan napupunta ang pera mo.\n\n"
            "Handa ka na bang magpatuloy? {billing_url}"
        ),
    },
    "sw": {
        "budget_exhausted": (
            "Umefika kikomo cha mwezi."
            " ${remaining} zimebaki."
            " Ujumbe mpya umezuiwa hadi upya wa mwezi ujao."
            "{plus_message}"
            " Fungua Bili kuboresha/kusimamia: {billing_url}"
        ),
        "waking_up": (
            "Msaidizi wako anaanza! \U0001f305"
            " Kawaida huchukua dakika moja hivi."
            " Tuma ujumbe wako tena baada ya muda mfupi!"
        ),
        "hibernation_waking": (
            "Msaidizi wako anarudi kutoka mapumziko! \u2600\ufe0f"
            " Ujumbe wako umepokelewa na utatumwa hivi karibuni."
            " Kawaida huchukua dakika moja hivi."
        ),
        "suspended": (
            "Msaidizi wako amesimamishwa."
            " Kuendesha wakala wa AI hugharimu pesa halisi"
            " \u2014 seva za wingu, tokeni za modeli, na hifadhi."
            " Tunaweka uwazi ili ujue pesa yako inaenda wapi.\n\n"
            "Uko tayari kuendelea? {billing_url}"
        ),
    },
}


def error_msg(lang: str, key: str, **kwargs: str) -> str:
    """Get a localized error message, falling back to English.

    Args:
        lang: Language code (e.g. "ja", "es"). Falls back to "en".
        key: Message key ("budget_exhausted", "waking_up", "suspended").
        **kwargs: Format placeholders (remaining, plus_message, billing_url).
    """
    msgs = ERROR_MESSAGES.get(lang, ERROR_MESSAGES["en"])
    template = msgs.get(key, ERROR_MESSAGES["en"][key])
    return template.format(**kwargs) if kwargs else template
