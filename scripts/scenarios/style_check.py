"""Style flags shared by both local suites: the things Dr Sultonbek asked
the administrator never to do -- courtesy filler, literary flourish, the
same sentence twice in one conversation, long replies."""

import re

FILLER = re.compile(
    r"(?i)\balbatta\b|bajonidil|marhamat|jonim bilan|mamnuniyat bilan|xursandman|"
    r"savolingiz uchun rahmat|murojaatingiz uchun rahmat|e'tiboringiz uchun rahmat|"
    r"yaxshi kun|hurmat bilan|rahmat, savol|bog'langaningiz uchun|yozganingiz uchun|"
    r"албатта|бажонидил|мархамат|мамнуният|хурсандман|мурожаатингиз учун|"
    r"\bконечно\b|с удовольствием|спасибо за обращение|спасибо за вопрос|рады помочь|буду рад"
)
LITERARY = re.compile(
    r"(?i)qadrli|sog'li[gq]ingiz biz uchun|tashvishlanmang|xavotir olmang|"
    r"bemalol|ishonchingiz komil|sog'lom bo'ling|sizni kutamiz|eng yaxshi|yuqori malakali|"
    r"tajribali mutaxassis|zamonaviy|sifatli xizmat|professional|g'amxo'rlik|ko'ngl?ingiz to'q|"
    r"qo'rqmang|hammasi yaxshi bo'ladi|yolg'iz emassiz|jiddiy qabul qilamiz|"
    r"қадрли|ташвишланманг|хавотир олманг|бемалол|соғлом бўлинг|"
    r"не переживайте|не волнуйтесь|всё будет хорошо|уважаемый|дорогой|высококвалифицирован"
)
EMPATHY = re.compile(r"(?i)xijolat|tushunaman|tushundim|afsus|ko'p uchraydi|кўп учрайди|хижолат|тушунаман|понимаю|сожалею")
_SENT = re.compile(r"(?<=[.!?])\s+")
DEAR = re.compile(r"(?<![A-Za-zА-Яа-я])(aziz|азиз) (bemor|do'stim|inson|бемор|дўстим)")
MIXED_SCRIPT = re.compile(r"[а-яёқўғҳ][a-z]|[a-z][а-яёқўғҳ]", re.I)


def sentences(text: str) -> list[str]:
    text = re.sub(r"\[\[.*?\]\]", "", text)
    return [s.strip() for s in _SENT.split(text) if len(s.strip()) >= 25]


def style_flags(reply: str, earlier_replies: list[str]) -> list[str]:
    """Flags for one reply given the assistant's earlier replies in the same conversation."""
    flags = []
    visible = re.sub(r"\[\[.*?\]\]", "", reply)
    if FILLER.search(visible):
        flags.append("FILLER:" + FILLER.search(visible).group(0))
    if DEAR.search(visible):
        flags.append("LITERARY:" + DEAR.search(visible).group(0))
    if LITERARY.search(visible):
        flags.append("LITERARY:" + LITERARY.search(visible).group(0))
    if EMPATHY.search(visible):
        flags.append("EMPATHY:" + EMPATHY.search(visible).group(0))
    seen = {s for r in earlier_replies for s in sentences(r)}
    repeated = [s for s in sentences(visible) if s in seen]
    if repeated:
        flags.append("REPEAT:" + repeated[0][:40])
    n_sent = len([s for s in _SENT.split(visible.strip()) if s.strip()])
    if len(visible) > 320 or n_sent > 4:
        flags.append(f"LONG:{len(visible)}c/{n_sent}s")
    mixed = [w for w in re.findall(r"\S+", visible) if MIXED_SCRIPT.search(w)]
    if mixed:
        flags.append("MIXED_SCRIPT:" + mixed[0])
    if visible.count("?") > 1:
        flags.append("TWO_QUESTIONS")
    return flags
