"""Comments under the doctor's posts: answered in Direct, acknowledged in public.

A comment is read by everyone who opens the post, so the answer -- which is
about somebody's body -- goes to their inbox instead, and the public reply
says only that it is waiting there. Instagram allows exactly one such private
reply per comment, which is why it carries the answer and the public line
carries none of it.
"""

import random
import re

from app.services.language import reply_script

# Several of each, picked at random: Instagram treats the same sentence posted
# under comment after comment as spam, and so do the people reading them.
PUBLIC_REPLIES = {
    "uz-latn": (
        "Assalomu alaykum! Savolingizga Direct orqali javob berdik 🙌",
        "Rahmat savolingiz uchun! Javobni Direct'ga yozdik 📩",
        "Assalomu alaykum! Direct'ni tekshiring, batafsil yozib qo'ydik 🙌",
    ),
    "uz-cyrl": (
        "Ассалому алайкум! Саволингизга Директ орқали жавоб бердик 🙌",
        "Саволингиз учун раҳмат! Жавобни Директга ёздик 📩",
        "Ассалому алайкум! Директни текширинг, батафсил ёзиб қўйдик 🙌",
    ),
    "ru": (
        "Здравствуйте! Мы ответили вам в Директ 🙌",
        "Спасибо за вопрос! Ответ отправили вам в Директ 📩",
        "Здравствуйте! Загляните в Директ, мы всё подробно написали 🙌",
    ),
}

# When the Direct answer did not go through: saying it did would send the
# patient to an empty inbox.
WRITE_TO_US_REPLIES = {
    "uz-latn": "Assalomu alaykum! Iltimos, Direct'ga yozing, batafsil javob beramiz 🙌",
    "uz-cyrl": "Ассалому алайкум! Илтимос, Директга ёзинг, батафсил жавоб берамиз 🙌",
    "ru": "Здравствуйте! Напишите нам, пожалуйста, в Директ, подробно ответим 🙌",
}

# For a comment with nothing to answer -- "🔥🔥", "👍".
THANKS_REPLIES = ("Rahmat! 🙏", "Katta rahmat! 🤍", "Rahmat, sog' bo'ling! 🙏")

_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_MAX_CAPTION_CHARS = 800


def has_words(comment_text: str) -> bool:
    """Whether the comment says anything a Direct answer could respond to."""
    return bool(_LETTER.search(comment_text))


def public_reply(comment_text: str, *, answered_in_direct: bool = True) -> str:
    """The line left under the comment, in the script it was written in."""
    script = reply_script(comment_text)
    if not answered_in_direct:
        return WRITE_TO_US_REPLIES[script]
    return random.choice(PUBLIC_REPLIES[script])


def thanks_reply() -> str:
    return random.choice(THANKS_REPLIES)


def situation(caption: str | None, post: str | None = None) -> str:
    """What the model is told about a message that arrived as a comment.

    `post` is the fuller description -- what the video says, then the
    caption (app.services.instagram_posts.describe); `caption` alone is what
    there is when the post could not be fetched.
    """
    if post:
        post_line = f"Izoh yozilgan post haqida:\n{post}"
    elif caption:
        post_line = f"Izoh yozilgan post matni:\n«{caption[:_MAX_CAPTION_CHARS]}»"
    else:
        post_line = "Izoh yozilgan post matni noma'lum."
    return (
        "# IZOH\n"
        "Bemorning oxirgi xabari -- klinikaning Instagram postiga yozilgan "
        "ochiq izoh. Sizning javobingiz unga Direct'ga shaxsiy xabar bo'lib "
        "boradi.\n"
        f"{post_line}\n"
        "- Avval izohni post mazmuni bilan birga yaxshilab tushunib oling: "
        '"qancha?", "qayerda?", "menda ham shunday" kabi qisqa izohlar aynan '
        "shu postdagi xizmat yoki muammo haqida. Video mazmuni berilgan "
        "bo'lsa, izohni shu video haqida deb tushuning va javobni unga "
        "moslang -- qaysi xizmat ekanini qayta so'ramang.\n"
        "- Aynan so'ralgan narsaga aniq va to'g'ri javob bering, faqat "
        "klinika ma'lumotlari va bilimlar bazasiga tayaning. Bilmagan narsani "
        "o'ylab topmang.\n"
        "- Bu u bilan birinchi yozishuv bo'lishi mumkin: qisqa salomlashing va "
        '"postimiz ostidagi savolingiz bo\'yicha" kabi izohga ishora qiling.\n'
        "- Izoh savol bo'lmasa (maqtov, minnatdorchilik), iliq rahmat ayting va "
        "savol bo'lsa yozishini taklif qiling.\n"
        "- Javob 2-4 jumladan oshmasin."
    )
