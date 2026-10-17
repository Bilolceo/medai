"""The lines the system writes into a transcript in the patient's place.

A patient who sends a photo, a voice note, a video or a reel has not written
anything, but the clinic still needs to see that they did -- so the inbound
edge records a short label instead: "📷 Rasm yubordi", "🎞 Video yubordi".
Those labels are the system's words, not the patient's, and everything that
reads the patient's words for meaning -- which language they write in, what
their complaint is, whether they said thanks -- has to skip them. Kept here,
in one place, so a new kind of label cannot be added without every reader
learning to skip it.
"""

PHOTO = "📷"
VOICE = "🎤"
VIDEO = "🎞"
FILE = "📎"
SHARE = "🔗"
STORY = "📣"
STICKER = "🏷"
REACTION = "💟"
# An Instagram comment, kept in the conversation with the commenter's own
# words after it -- the patient writing, so not one of the labels below.
COMMENT_PREFIX = "💬 Izoh: "

_PREFIXES = (PHOTO, VOICE, VIDEO, FILE, SHARE, STORY, STICKER, REACTION)

# Rendered in Uzbek Latin whatever the patient writes: these are read by the
# clinic in its dashboard, not by the patient.
TEXT = {
    VIDEO: "🎞 Video yubordi",
    FILE: "📎 Fayl yubordi",
    SHARE: "🔗 Post yoki reels ulashdi",
    STORY: "📣 Storisida klinikani belgiladi",
    STICKER: "🏷 Stiker yubordi",
    REACTION: "💟 Reaksiya bildirdi",
}
UNSUPPORTED = "📎 Instagram ko'rsatmaydigan xabar yubordi"


def is_label(text: str | None) -> bool:
    """Whether this transcript line was written by the system, not typed."""
    return (text or "").lstrip().startswith(_PREFIXES)


def reaction(emoji: str | None) -> str:
    shown = (emoji or "").strip()[:8]
    return f"{TEXT[REACTION]}: {shown}" if shown else TEXT[REACTION]
