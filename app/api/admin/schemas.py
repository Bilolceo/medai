"""Request and response shapes for the admin API.

Explicit models rather than returning ORM rows: what a dashboard needs to
show is not the same as what a table stores, and a route that serialises the
model directly leaks every column it later gains — including ones nobody
meant to publish, like a channel's credentials.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.core.roles import Permission
from app.models.appointment import AppointmentStatus
from app.models.lead import LeadStatus
from app.services.persona import MAX_EXAMPLES_LENGTH, MAX_PROMPT_LENGTH


def _not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


# --- session ---------------------------------------------------------------


class SessionInfo(BaseModel):
    operator_id: uuid.UUID
    name: str
    role: str
    tenant_id: uuid.UUID
    tenant_name: str
    # What this account may do, resolved from its role by app.core.roles.
    # Sent so the dashboard can hide a control it would only be refused for,
    # and sent as permissions rather than as a role so the mapping lives in
    # one place: a UI that re-derives "operators may edit the FAQ" from the
    # role string is a second copy of the rules, and the copy that drifts is
    # always the one that is not enforcing anything.
    permissions: list[Permission]
    # The token every mutating request must echo back in X-CSRF-Token.
    csrf_token: str
    # The clinic's Google Sheet, when one is configured: the report, the
    # leads and the appointment book are mirrored there, and the dashboard
    # links to it. None when no sheet is set up.
    sheet_url: str | None = None


# --- conversations ---------------------------------------------------------


class ConversationCounts(BaseModel):
    taken_over: int
    needs_doctor: int
    comments: int = 0
    bot_only: int = 0


class ConversationSummary(BaseModel):
    id: uuid.UUID
    status: str
    is_bot_enabled: bool
    patient_name: str | None
    # The handle the dashboard labels this conversation with. None until the
    # lookup lands (app.services.profile), which is why patient_external_id
    # is still sent: it is the fallback, not the label.
    patient_username: str | None
    patient_external_id: str
    channel: str
    last_message_at: datetime | None
    last_message_preview: str | None
    updated_at: datetime
    # Set while a person owes this patient a reply; the list pins these.
    needs_doctor_since: datetime | None = None
    # For a pinned conversation: what the patient actually asked -- their
    # newest message with something in it, not the "Rahmat!" that usually
    # comes last. The task list shows this so nobody has to open the chat
    # to know what it is about.
    waiting_reason: str | None = None
    # For a conversation that began under a post: which one, in a few words.
    post_summary: str | None = None
    post_media_id: str | None = None


class PostOut(BaseModel):
    """The Instagram post a comment was left under, as the dashboard shows it."""

    media_id: str
    media_type: str | None
    caption: str | None
    summary: str | None
    permalink: str | None
    posted_at: datetime | None
    # Whether a cover can be fetched from /api/admin/posts/{media_id}/cover.
    has_cover: bool
    transcript_status: str


class PostCommentGroup(BaseModel):
    """One post and how many conversations began with a comment under it."""

    post: PostOut
    comments: int


class MessageOut(BaseModel):
    id: uuid.UUID
    sender: str
    content: str
    channel: str
    created_at: datetime
    # For a comment: the post it was written under.
    post: PostOut | None = None
    # "sent", "undeliverable", "failed". An answer that never reached the
    # patient is the thing the clinic most needs to see in a transcript,
    # and it used not to be written down at all.
    delivery_status: str = "sent"
    delivery_error: str | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationSummary
    messages: list[MessageOut]


class BotToggle(BaseModel):
    is_bot_enabled: bool


class OperatorReply(BaseModel):
    text: str

    _check = field_validator("text")(_not_blank)


# --- appointments ----------------------------------------------------------


class AppointmentOut(BaseModel):
    id: uuid.UUID
    scheduled_at: datetime
    doctor_id: uuid.UUID | None
    doctor_name: str
    patient_name: str | None
    patient_phone: str | None
    notes: str | None
    status: str
    source: str


class AppointmentCreate(BaseModel):
    scheduled_at: datetime
    patient_name: str
    patient_phone: str | None = None
    doctor_id: uuid.UUID | None = None
    notes: str | None = None

    _check = field_validator("patient_name")(_not_blank)


# --- doctors ---------------------------------------------------------------


class DoctorOut(BaseModel):
    id: uuid.UUID
    name: str
    specialty: str
    phone: str | None
    working_hours: str
    is_active: bool


class DoctorCreate(BaseModel):
    name: str
    specialty: str = "Urolog"
    phone: str | None = None
    working_hours: str = "09:00 - 18:00"

    _check = field_validator("name")(_not_blank)


class DoctorUpdate(BaseModel):
    name: str | None = None
    specialty: str | None = None
    phone: str | None = None
    working_hours: str | None = None
    is_active: bool | None = None


# --- leads -----------------------------------------------------------------


class LeadOut(BaseModel):
    id: uuid.UUID
    patient_name: str | None
    phone: str | None
    topic: str | None
    convenient_time: str | None
    status: str
    notes: str | None
    created_at: datetime
    # The patient's Instagram handle and conversation, when the lead came
    # from a chat, so the "Nomer" screen can name them and open the thread.
    username: str | None = None
    conversation_id: uuid.UUID | None = None


class LeadCreate(BaseModel):
    patient_name: str | None = None
    phone: str | None = None
    topic: str | None = None
    convenient_time: str | None = None
    notes: str | None = None


class LeadUpdate(BaseModel):
    status: LeadStatus | None = None
    patient_name: str | None = None
    phone: str | None = None
    topic: str | None = None
    convenient_time: str | None = None
    notes: str | None = None


# --- knowledge base --------------------------------------------------------


class FaqOut(BaseModel):
    id: uuid.UUID
    question: str
    answer: str
    category: str | None
    is_active: bool


class RuleOut(BaseModel):
    number: int
    text: str
    # True: the assistant reads it on every reply. False: only an old copy.
    online: bool


class RuleRemove(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class RuleState(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    online: bool


class FaqCreate(BaseModel):
    question: str
    answer: str
    category: str | None = None

    _check_q = field_validator("question")(_not_blank)
    _check_a = field_validator("answer")(_not_blank)


# --- settings --------------------------------------------------------------


class TenantSettings(BaseModel):
    """The per-clinic settings the dashboard can edit.

    Stored in tenants.settings (JSONB) rather than as columns, which is the
    pattern the Telegram side established and the one that lets a clinic tune
    its own wording without a migration. Unset fields are left as they were,
    so a form that only changes the address does not blank the phone numbers.
    """

    # The clinic's own on/off switch for the assistant, flipped from the
    # conversations screen. False and every inbound message is still received,
    # still stored and still listed there -- no reply is written and no job is
    # queued for one, so nothing piles up to be answered late. It lives here,
    # in the clinic's settings, because the alternative was reaching into the
    # host's dashboard and stopping the worker, which takes minutes, loses the
    # answer to anything already queued, and is not something a front desk can
    # be asked to do at eleven at night.
    bot_replies_enabled: bool | None = None
    # Answers to comments under the clinic's posts, on their own switch
    # (owner, 2026-10-20): off, a comment is still recorded and listed under
    # "Izohlar" for staff, and nothing is posted or sent to its author.
    comment_replies_enabled: bool | None = None
    debounce_seconds: int | None = Field(default=None, ge=0, le=300)
    clinic_address: str | None = None
    clinic_landmark: str | None = None
    clinic_phone_numbers: str | None = None
    clinic_work_hours: str | None = None
    clinic_latitude: float | None = Field(default=None, ge=-90, le=90)
    clinic_longitude: float | None = Field(default=None, ge=-180, le=180)
    strict_rules: list[str] | None = None
    # The bot's persona and its example conversations, in the clinic's own
    # words (see app.services.persona). null puts the default back; for the
    # examples an empty string is a choice -- no examples -- and is kept.
    assistant_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_LENGTH)
    assistant_examples: str | None = Field(default=None, max_length=MAX_EXAMPLES_LENGTH)


class AssistantDefaults(BaseModel):
    """What the bot uses until the clinic writes its own."""

    prompt: str
    examples: str


# --- analytics -------------------------------------------------------------


class DayCount(BaseModel):
    day: date
    count: int


class AnalyticsSummary(BaseModel):
    conversations_total: int
    conversations_open: int
    conversations_with_operator: int
    appointments_total: int
    appointments_active: int
    leads_total: int
    leads_new: int
    faqs_active: int
    appointments_by_day: list[DayCount]


ACTIVE_APPOINTMENT_STATUSES = {AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED}


class SuggestionEvidence(BaseModel):
    conversation_id: str
    quote: str = ""
    # Who the conversation was with, so the card can say it without a lookup.
    patient: str | None = None


class SuggestionOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    problem: str
    rule_text: str | None
    question: str | None
    answer: str | None
    evidence: list[SuggestionEvidence]
    created_at: datetime
    decided_at: datetime | None


class SuggestionAccept(BaseModel):
    """What the clinic accepts -- the proposal, or its own edit of it."""

    rule_text: str | None = Field(default=None, max_length=4000)
    question: str | None = Field(default=None, max_length=4000)
    answer: str | None = Field(default=None, max_length=4000)


class ReviewStatus(BaseModel):
    pending: int
    last_run: datetime | None
