"""Short strings the forum profile adds (discussion question, link label), per language.
Written for this project; a native speaker should review them before production use."""

DISCUSSION = {
    "en": {
        "title_q": "what do you think?",
        "body_q": "What has your experience been, and what would you do differently?",
        "link": "Link",
    },
    "es": {
        "title_q": "¿qué opinan?",
        "body_q": "¿Cuál ha sido su experiencia y qué harían diferente?",
        "link": "Enlace",
    },
    "de": {
        "title_q": "was meint ihr?",
        "body_q": "Welche Erfahrungen habt ihr gemacht, und was würdet ihr anders machen?",
        "link": "Link",
    },
    "fr": {
        "title_q": "qu'en pensez-vous ?",
        "body_q": "Quelle est votre expérience, et que feriez-vous autrement ?",
        "link": "Lien",
    },
    "pt": {
        "title_q": "o que vocês acham?",
        "body_q": "Qual tem sido a experiência de vocês, e o que fariam diferente?",
        "link": "Link",
    },
    "hi": {
        "title_q": "आपकी क्या राय है?",
        "body_q": "आपका अनुभव कैसा रहा है?",
        "link": "लिंक",
    },
    "ja": {
        "title_q": "どう思いますか？",
        "body_q": "皆さんの経験はいかがですか？",
        "link": "リンク",
    },
    "ta": {
        "title_q": "உங்கள் கருத்து என்ன?",
        "body_q": "உங்கள் அனுபவம் எப்படி இருந்தது?",
        "link": "இணைப்பு",
    },
}


def discussion(lang: str | None) -> dict[str, str]:
    return DISCUSSION.get(lang or "en", DISCUSSION["en"])
