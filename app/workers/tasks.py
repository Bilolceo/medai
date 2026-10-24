"""Background jobs: turn a recorded inbound message into a sent reply.

Platform-neutral. The job arguments carry a channel id rather than anything
Instagram-shaped, the reply goes out through app.services.delivery (which
dispatches on the channel's type), and every step in between — retrieval,
the answer prompt, the transcript — is shared business logic.
The Telegram bot's inbound edge enqueues these same two jobs.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from arq import Retry, cron
from arq.connections import RedisSettings
from arq.worker import func
from sqlalchemy import func as sql
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for the side effect of registering the built-in channel adapters,
# which app.services.delivery then looks up by channel type. The worker is a
# separate process from the web app, so it must do this itself.
from app import channels  # noqa: F401  - importing it registers the adapters
from app.channels.base import ChannelAdapter
from app.channels.instagram.client import InstagramClient, get_instagram_client
from app.core.config import get_settings
from app.core.db import db_session
from app.core.encryption import decrypt
from app.core.logging import configure_logging
from app.core.redaction import preview
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.appointment import Appointment
from app.models.channel import Channel
from app.models.conversation import Conversation
from app.models.message import DeliveryStatus, Message, MessageSender
from app.models.patient_media import PatientMedia
from app.models.tenant import Tenant
from app.models.user import User
from app.rag.embeddings import EmbeddingProvider
from app.rag.llm import LLMProvider, get_comment_llm_provider, get_llm_provider
from app.repositories.channel import ChannelRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.user import UserRepository
from app.services import (
    booking_offer,
    callbacks,
    clinic_echoes,
    comments,
    doctor_telegram,
    handoff,
    instagram_posts,
    message_labels,
    patient_media,
    small_talk,
    turn,
    voice_notes,
)
from app.services.admin_commands import handle_instruction, is_admin, parse_rule
from app.services.answer import generate_answer
from app.services.appointment import CLINIC_TIMEZONE
from app.services.complaints import patient_words
from app.services.conversation import (
    context_for_reply,
    last_inbound_at,
    record_outbound_message,
    register_inbound_message,
    reply_context_for,
)
from app.services.conversation_signals import find_phone_number
from app.services.debounce import (
    join_messages,
    pop_batch_if_current_generation,
    restore_batch,
)
from app.services.delivery import send_reply
from app.services.profile import ensure_instagram_username
from app.services.reminders import send_due_reminders
from app.services.review import review_window_start, run_review
from app.services.sheets import (
    AppointmentRow,
    LeadRow,
    label_appointment_weekdays,
    mirror_appointment,
    mirror_lead,
    summarise_problem,
)
from app.services.tenant_resolution import (
    bot_replies_enabled,
    comment_replies_enabled,
    debounce_window,
)
from app.services.token_refresh import refresh_instagram_tokens
from app.services.webhook_watch import verify_telegram_webhooks

logger = logging.getLogger(__name__)

# How long to wait before each retry of a failed batch, in order. The first
# few are short because most failures are a blip; the last is long enough to
# ride out a provider having a bad few minutes. Shorter in total than the TTL
# restore_batch sets, or the last attempt would find nothing to answer.
_RETRY_BACKOFF_SECONDS = (30, 120, 300, 600)

# Must match arq's max_tries in WorkerSettings: after this many attempts the
# job is abandoned, and the log line at that point is the only notice anyone
# gets that a patient went unanswered.
_MAX_ATTEMPTS = 5

# The per-conversation reply lock. It expires on its own after the first
# figure, so a worker that died holding it cannot silence a patient; the
# second is how long a batch waits for the reply before it before giving up
# into the retry ladder above.
_REPLY_LOCK_SECONDS = 240
_REPLY_LOCK_WAIT_SECONDS = 200


async def _may_answer(session: AsyncSession, *, channel_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether the assistant may answer this patient at all.

    Normally yes. While ANSWER_ONLY_USERNAMES is set -- a test on the live
    account -- only those handles are answered; everybody else's message is
    still recorded and visible in the dashboard, and simply left for a person.
    """
    allowed = get_settings().answer_only_usernames
    if not allowed:
        return True
    username = await ensure_instagram_username(session, channel_id=channel_id, user_id=user_id)
    await session.commit()
    if is_admin(username, allowed):
        return True
    logger.info("reply_withheld_not_on_test_list", extra={"username": username})
    return False


async def _held_back(
    session: AsyncSession,
    redis: Any,
    *,
    conversation: Conversation,
    channel_id: str,
    patient_id: str,
    answering_up_to: datetime | None,
    may_wait: bool,
) -> str | None:
    """Why a written reply should not go out now, or None to send it.

    The model can think for most of a minute. In that minute somebody from
    the clinic may answer -- a person, or the account's own automatic reply
    -- and the patient may write again. Live, 2026-10-16: two answers to
    one question, and the assistant's reply landing under the operator's.
    """
    await session.refresh(conversation, ["is_bot_enabled"])
    if not conversation.is_bot_enabled:
        return "reply_dropped_clinic_took_over"
    if answering_up_to is None:
        return None
    later = (
        await session.execute(
            select(Message.sender, Message.content).where(
                Message.conversation_id == conversation.id,
                Message.created_at > answering_up_to,
            )
        )
    ).all()
    if any(sender == MessageSender.OPERATOR for sender, _ in later):
        return "reply_dropped_clinic_took_over"
    if redis is not None:
        try:
            sent = await clinic_echoes.since(redis, channel_id, patient_id, answering_up_to)
        except Exception:  # noqa: BLE001 - without the note, send as before
            logger.exception("clinic_echo_read_failed")
            sent = []
        if sent:
            ours = (
                (
                    await session.execute(
                        select(Message.content).where(
                            Message.conversation_id == conversation.id,
                            Message.sender == MessageSender.BOT,
                            Message.created_at >= datetime.now(UTC) - timedelta(minutes=15),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if any(not any(handoff.same_message(t, o) for o in ours) for t in sent):
                return "reply_dropped_clinic_took_over"
    if may_wait and any(
        sender == MessageSender.PATIENT
        and not message_labels.is_label(content)
        and not small_talk.is_acknowledgement(content)
        for sender, content in later
    ):
        # The next batch is queued behind this one and sees both messages:
        # one answer to all of it, not two.
        return "reply_superseded_patient_wrote_more"
    return None


async def process_inbound_message(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    conversation_id: str,
    sender_external_id: str,
    message_text: str,
    reply_context: Mapping[str, Any] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    adapter: ChannelAdapter | None = None,
) -> None:
    """ARQ job: answer one inbound message (or one debounced batch) and send
    the reply back over the channel it arrived on.

    Runs in the worker process, not the request that received the webhook —
    that request's DB session and tenant context don't survive past its 200
    response, so this re-establishes both from the plain, serializable
    arguments the inbound edge enqueued (a UUID isn't one of those, hence
    the ids arrive as str and are parsed back here).

    The channel id is carried explicitly rather than looked up from the
    tenant, so the reply always goes out over the account the patient
    actually wrote to — see app.services.delivery for what the previous
    "first active channel" lookup got wrong.

    session_factory defaults to app.core.db.db_session (a genuinely fresh
    session against the real engine — in production this job never receives
    one from a caller). It is injectable for the same reason
    embedding_provider/llm_provider/adapter are: tests call this function
    directly, bypassing the queue. That is not just convenience — a *second*,
    independent db_session() in a test cannot see that test's own uncommitted
    fixture data, so tests must be able to point the job at the same
    transactional session the test itself is using.
    """
    tenant_uuid = uuid.UUID(tenant_id)
    conversation_uuid = uuid.UUID(conversation_id)
    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            if not await bot_replies_enabled(session, tenant_uuid):
                # Belt to the webhook's braces. The webhook stops queueing the
                # moment the clinic switches the assistant off, but jobs
                # already in Redis outlive that -- including any scheduled
                # before the switch was flipped. Checked again here so
                # switching off is immediate rather than "immediate once the
                # queue drains".
                logger.info(
                    "worker_bot_disabled_for_tenant",
                    extra={"tenant_id": tenant_id, "conversation_id": conversation_id},
                )
                return
            conversation_row = await session.get(Conversation, conversation_uuid)
            if conversation_row is not None and not await _may_answer(
                session, channel_id=uuid.UUID(channel_id), user_id=conversation_row.user_id
            ):
                return
            history = await context_for_reply(session, conversation_uuid)
            conversation = await session.get(Conversation, conversation_uuid)
            channel = await session.get(Channel, uuid.UUID(channel_id))
            # Bound before the branch: the spreadsheet mirror below reads it
            # whether or not a booking happened, and a conversation the job
            # cannot load would otherwise raise NameError there — after the
            # patient has already been answered.
            appointment: Appointment | None = None
            if conversation is None:  # pragma: no cover - defensive
                logger.error("conversation_missing", extra={"conversation_id": conversation_id})
                return

            # The newest patient message this reply answers: anything after it
            # -- from the clinic or the patient -- is checked before sending.
            answering_up_to = await last_inbound_at(session, conversation_uuid)

            # One turn: the model writes the reply and any booking it made is
            # written down. See app.services.turn.
            turn_result = await turn.respond(
                session,
                conversation_id=conversation_uuid,
                user_id=conversation.user_id,
                message=message_text,
                history=history,
                # "instagram" / "telegram", so the dashboard's SOURCE column
                # says where the booking came from — beside "operator" for
                # the ones staff enter by hand.
                source=str(channel.type) if channel is not None else "bot",
                embedding_provider=embedding_provider,
                llm_provider=llm_provider,
            )
            if turn_result.silent:
                # A 👍 or "rahmat" that closed the exchange: nothing to send.
                await session.commit()
                return
            reply = turn_result.reply
            appointment = turn_result.appointment
            # Committed now, like a booking: the callback below can roll the
            # session back, and the pin must not go with it.
            if appointment is not None or turn_result.needs_doctor:
                await session.commit()

            # A patient who asked to be rung. Taken from the assistant's
            # marker when it wrote one, otherwise from the patient's own words;
            # never while they are being booked, which is its own record.
            reply, callback = callbacks.extract(reply)
            if conversation is not None and appointment is None:
                said = [turn["content"] for turn in history if turn["role"] == "user"]
                said.append(message_text)
                callback = callback or callbacks.from_patient_words(said)
                if callback is not None:
                    try:
                        await callbacks.record(
                            session,
                            user=await session.get(User, conversation.user_id),
                            conversation_id=conversation_uuid,
                            request=callback,
                            fallback_reason=summarise_problem(said) or None,
                        )
                        await session.commit()
                    except Exception:  # noqa: BLE001 - the reply must still go out
                        await session.rollback()
                        logger.exception("callback_record_failed")

            # Full reply text stays out of INFO — it is patient-adjacent
            # content that should not sit in logs that may ship to external
            # monitoring (TZ section 7, personal data). Length + truncated
            # preview at INFO; full text only at DEBUG, under a distinct
            # event name so it is never ambiguous with the redacted INFO line.
            logger.info(
                "webhook_reply_generated",
                extra={
                    "tenant_id": tenant_id,
                    "conversation_id": conversation_id,
                    "sender_external_id": sender_external_id,
                    "history_turns": len(history),
                    "reply_length": len(reply),
                    "reply_preview": preview(reply),
                },
            )
            logger.debug(
                "webhook_reply_full_text",
                extra={"sender_external_id": sender_external_id, "reply": reply},
            )

            # The real recorded time the patient last wrote, now that inbound
            # messages are persisted — not the "assume now" placeholder this
            # used before, which left the reply-window check unable to
            # observe staleness at all. Falling back to now() only covers a
            # conversation whose inbound row is somehow missing, where
            # refusing to reply would be the worse failure.
            patient_last_wrote = await last_inbound_at(session, conversation_uuid)

            # The clinic's own spreadsheet, for the owners who never open a
            # dashboard. Only when there is something worth a row -- a phone
            # number the patient typed, or a booking -- because a row per
            # message would be a page of the same person, and the sheet is
            # keyed on the number.
            patient_said = [turn["content"] for turn in history if turn["role"] == "user"]
            patient_said.append(message_text)
            phone = next(
                (found for text in patient_said if (found := find_phone_number(text))), None
            )
            if callback is not None:
                phone = callback.phone
            # Why they are coming, in the order of how reliably each says it.
            #
            # The assistant asks every patient for the reason before it books
            # them and carries their answer in the marker, so a booked
            # patient's own answer to that question is the best line there
            # is. Without it the first substantive thing they wrote is a
            # guess -- and on a conversation that opened "Assalomu alaykum /
            # Asadbek Risqiyev / 93 444 444 / buyrak og'rig'i" the guess was
            # the greeting, which told the clinic nothing at all.
            visit_reason = (
                patient_words(appointment.notes) if appointment is not None else ""
            ) or summarise_problem(patient_said)
            lead = (
                LeadRow(
                    name=appointment.patient_name if appointment is not None else None,
                    phone=phone,
                    source=str(channel.type) if channel is not None else "bot",
                    # The time they asked to be rung goes with it: it is the
                    # first thing the operator needs from the row (the owner,
                    # 2026-10-17: "soat nechida telefon qilish kerak").
                    comment=" · ".join(
                        part
                        for part in (
                            (callback.reason if callback else None) or visit_reason,
                            f"Qo'ng'iroq: {callback.when}" if callback and callback.when else "",
                        )
                        if part
                    ),
                    # The day of the visit, not the day they wrote. Somebody
                    # who messages on the 28th to be seen on the 31st belongs
                    # in the 31st's list, because that is the list the front
                    # desk works from that morning. With nothing booked yet,
                    # today is the only day they belong to.
                    day=(
                        appointment.scheduled_at.astimezone(CLINIC_TIMEZONE).date()
                        if appointment is not None
                        else datetime.now(UTC).astimezone(CLINIC_TIMEZONE).date()
                    ),
                )
                if phone or appointment is not None
                else None
            )

            # The model can think for most of a minute, and an operator who
            # answers in that minute has taken the conversation (seen
            # 2026-10-16: "Qabulga yozilish uchun: ☎..." and then the bot's
            # own offer under it). What the turn wrote down -- a booking, a
            # callback -- stays; only the second voice is held back.
            held = await _held_back(
                session,
                ctx.get("redis"),
                conversation=conversation,
                channel_id=channel_id,
                patient_id=sender_external_id,
                answering_up_to=answering_up_to,
                # A booking or a callback the patient must hear about goes
                # out even when they have written more since.
                may_wait=appointment is None,
            )
            if held is not None:
                await session.commit()
                logger.info(held, extra={"conversation_id": conversation_id})
                return

            delivery_error: str | None = None
            try:
                delivered_over = await send_reply(
                    session,
                    channel_id=uuid.UUID(channel_id),
                    recipient_external_id=sender_external_id,
                    text=reply,
                    last_user_message_at=patient_last_wrote or datetime.now(UTC),
                    reply_context=reply_context,
                    adapter=adapter,
                )
            except Exception as exc:
                # Recorded as failed and re-raised: the job is retried, and
                # meanwhile the clinic can see in the dashboard that an
                # answer was written and did not arrive. Silence here is
                # what made a broken account look like an idle bot.
                delivery_error = f"{type(exc).__name__}: {exc}"
                await record_outbound_message(
                    session,
                    conversation_id=conversation_uuid,
                    channel_type=str(channel.type) if channel is not None else "bot",
                    text=reply,
                    status=DeliveryStatus.FAILED,
                    error=delivery_error,
                )
                await session.commit()
                raise
            finally:
                # In a finally, and after the send rather than before it, for
                # two different reasons.
                #
                # After: writing to Google takes a round trip the patient
                # would otherwise spend waiting for their answer.
                #
                # Regardless: a patient who left a number is a patient the
                # clinic wants to ring, and a delivery that failed -- a
                # blocked bot, an expired token, a closed messaging window --
                # is the case where they want to ring them *most*. Skipping
                # the row exactly then would lose the lead the reply could
                # not reach. mirror_lead never raises, so this cannot mask
                # the delivery error it runs beside.
                if lead is not None:
                    await mirror_lead(lead)
                # The booking itself, with the time on it. The lead list
                # says somebody wants to come; the appointment book says
                # when, with whom, and whether anyone has confirmed it --
                # which is what the front desk works from in the morning.
                if appointment is not None:
                    await mirror_appointment(
                        AppointmentRow(
                            appointment_id=appointment.id,
                            created_at=appointment.created_at or datetime.now(UTC),
                            scheduled_at=appointment.scheduled_at,
                            patient_name=appointment.patient_name,
                            phone=appointment.patient_phone or phone,
                            doctor=appointment.doctor_name,
                            channel=str(channel.type) if channel is not None else "web",
                            client_id=sender_external_id,
                            status=appointment.status,
                            note=visit_reason,
                        )
                    )

            # Recorded either way. Delivered, it is part of the
            # conversation; undelivered -- outside Instagram's 24-hour
            # window, or a channel switched off -- it is the thing the
            # clinic most needs to see, and used to be the one thing that
            # was never written down. The model's history still reads only
            # what was delivered (app.repositories.message.list_recent).
            await record_outbound_message(
                session,
                conversation_id=conversation_uuid,
                channel_type=delivered_over or (str(channel.type) if channel else "bot"),
                text=reply,
                status=(
                    DeliveryStatus.SENT
                    if delivered_over is not None
                    else DeliveryStatus.UNDELIVERABLE
                ),
                error=None if delivered_over is not None else "not delivered",
            )
            await session.commit()
    finally:
        reset_current_tenant(token)


async def fire_debounce_window(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    conversation_id: str,
    sender_external_id: str,
    generation: int,
    reply_context: Mapping[str, Any] | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    adapter: ChannelAdapter | None = None,
) -> None:
    """ARQ job: fires once a patient's debounce window has elapsed with no
    further messages. Scheduled (deferred) by
    app.services.debounce.handle_inbound_message for every
    message; most scheduled calls for a burst of messages from the same
    patient are stale by the time they run (a later message reset the
    window) and no-op here via pop_batch_if_current_generation — only the
    last one scheduled for the current generation actually claims and
    processes the batch.

    Uses ctx["redis"] (the worker's own pool, set by arq itself) rather than
    a second cached pool, and hands the claimed batch to
    process_inbound_message unchanged — same generate-and-send logic,
    whether it is answering one message or a joined batch of several.
    """
    pool = ctx["redis"]
    messages = await pop_batch_if_current_generation(
        pool, uuid.UUID(tenant_id), uuid.UUID(channel_id), sender_external_id, generation
    )
    if not messages:
        return

    try:
        # One reply at a time per conversation, from reading the history to
        # recording the answer. A patient who writes again while the first
        # answer is being generated opens a second batch; without this the
        # two ran side by side, the second read a history without the first
        # answer in it, and the patient got two replies that talked past each
        # other -- "booked you at 11:00" and then "11:00 is taken".
        async with pool.lock(
            f"reply-lock:{conversation_id}",
            timeout=_REPLY_LOCK_SECONDS,
            blocking_timeout=_REPLY_LOCK_WAIT_SECONDS,
        ):
            await process_inbound_message(
                ctx,
                tenant_id,
                channel_id,
                conversation_id,
                sender_external_id,
                join_messages(messages),
                reply_context,
                session_factory=session_factory,
                embedding_provider=embedding_provider,
                llm_provider=llm_provider,
                adapter=adapter,
            )
    except Exception as exc:
        # The claim above emptied Redis, so these words exist nowhere else.
        # Put them back before letting the failure out, or a rate-limited
        # model means a patient wrote and nobody ever answered -- and nothing
        # anywhere records that it happened.
        await restore_batch(
            pool,
            uuid.UUID(tenant_id),
            uuid.UUID(channel_id),
            sender_external_id,
            generation,
            messages,
        )
        attempt = int(ctx.get("job_try") or 1)
        defer = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS)) - 1]
        if attempt >= _MAX_ATTEMPTS:
            # The last word on this batch. It stays in Redis until its TTL,
            # but nothing will come back for it, so this line is the only
            # place the clinic can learn the patient was left unanswered.
            logger.error(
                "debounce_batch_abandoned attempts=%d messages=%d sender=%s error=%s",
                attempt,
                len(messages),
                sender_external_id,
                exc,
            )
            # And on the dashboard: a log line is read by nobody, while a
            # conversation pinned "Doktor javobi kerak" is answered by a
            # person the same day. Best effort -- a failure here must not
            # hide the one above.
            with suppress(Exception):
                token = set_current_tenant(uuid.UUID(tenant_id))
                try:
                    async with session_factory() as session:
                        await handoff.flag(session, uuid.UUID(conversation_id))
                        await session.commit()
                finally:
                    reset_current_tenant(token)
            raise
        logger.warning(
            "debounce_batch_deferred attempt=%d defer=%ds messages=%d error=%s",
            attempt,
            defer,
            len(messages),
            exc,
        )
        raise Retry(defer=defer) from exc


async def send_appointment_reminders(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    now: datetime | None = None,
    adapter: ChannelAdapter | None = None,
) -> None:
    """Cron job: remind patients about appointments that are coming up.

    Runs across every tenant — it has no request and no operator to take one
    from — and app.services.reminders sets the tenant per appointment before
    touching anything scoped. See that module for why a reminder is marked
    sent only once it has actually gone out.
    """
    async with session_factory() as session:
        run = await send_due_reminders(session, now=now, adapter=adapter)

    if run.sent or run.failed or run.skipped:
        logger.info(
            "appointment_reminders_run",
            extra={"sent": run.sent, "failed": run.failed, "skipped": run.skipped},
        )


async def refresh_channel_tokens(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
) -> None:
    """Cron job: renew Instagram tokens before they run out.

    Daily, though a token lasts sixty days. The margin is the point: a job
    that runs far more often than it needs to can miss most of its runs and
    still never let a clinic's Instagram go quiet.
    """
    async with session_factory() as session:
        run = await refresh_instagram_tokens(session)
    if run.touched:
        logger.info(
            "instagram_token_refresh_run refreshed=%d skipped=%d failed=%d",
            run.refreshed,
            run.skipped,
            run.failed,
        )


async def verify_channel_webhooks(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
) -> None:
    """Cron job: make sure Telegram is still delivering to this deployment.

    Unlike the token job, this guards against something that can happen at
    any moment rather than on a known schedule -- anything holding the bot
    token can clear the registration, and nothing on this side notices,
    because the symptom is the absence of traffic. Frequent and cheap: the
    check is one read-only call per bot and only writes when the answer is
    wrong.
    """
    async with session_factory() as session:
        run = await verify_telegram_webhooks(
            session, public_base_url=get_settings().public_base_url
        )
    if run.notable:
        logger.info(
            "telegram_webhook_watch_run ok=%d repaired=%d skipped=%d failed=%d",
            run.ok,
            run.repaired,
            run.skipped,
            run.failed,
        )


async def apply_admin_rule(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    user_id: str,
    sender_external_id: str,
    message_text: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    adapter: ChannelAdapter | None = None,
    llm_provider: LLMProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> None:
    """ARQ job: understand and store an instruction the clinic's admin sent by DM.

    The webhook decides only that this *looks* like an admin command -- the
    message starts with the keyword -- and everything that costs anything
    happens here: the handle is resolved if it is not known yet, the sender is
    checked against the nominated admins, the rule is stored, and the admin is
    told what the assistant now believes.

    Verification lives here rather than in the webhook because it may need a
    call to Meta to learn who is writing, and a webhook that waits on Meta is
    a webhook Meta retries. A message that turns out not to be from an admin
    is dropped with a log line and nothing else: whoever sent it already got
    the ordinary reply, since the webhook queued that too.
    """
    settings = get_settings()
    tenant_uuid = uuid.UUID(tenant_id)
    rule = parse_rule(message_text, settings.admin_command_keyword)
    if rule is None:
        return

    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            username = await ensure_instagram_username(
                session, channel_id=uuid.UUID(channel_id), user_id=uuid.UUID(user_id)
            )
            await session.commit()
            if not is_admin(username, settings.admin_usernames):
                logger.warning(
                    "admin_rule_refused",
                    extra={
                        "tenant_id": tenant_id,
                        "sender_external_id": sender_external_id,
                        "username": username,
                    },
                )
                return

            # Understood, not copied: see app.services.rule_interpreter. What
            # the admin is told is what was actually stored.
            confirmation = await handle_instruction(
                session,
                tenant_id=tenant_uuid,
                instruction=rule,
                provider=llm_provider or get_llm_provider(),
                embedding_provider=embedding_provider,
            )
            await session.commit()

            await send_reply(
                session,
                channel_id=uuid.UUID(channel_id),
                recipient_external_id=sender_external_id,
                text=confirmation,
                last_user_message_at=datetime.now(UTC),
                adapter=adapter,
            )
            logger.info(
                "admin_instruction_handled", extra={"tenant_id": tenant_id, "username": username}
            )
    finally:
        reset_current_tenant(token)


async def apply_owner_rule(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    message_text: str,
    recipient_external_id: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    client: InstagramClient | None = None,
    adapter: ChannelAdapter | None = None,
    llm_provider: LLMProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> None:
    """ARQ job: understand and store an instruction the account's own owner typed.

    The doctor's inbox is the doctor's own Instagram, and nobody can message
    themselves -- so the rule arrives as an echo of a message the owner sent
    into some other conversation. Anybody logged in as the account could have
    sent it, which is why the account's handle must still be a nominated
    admin: the switch for this stays ADMIN_INSTAGRAM_USERNAMES, as it is for
    rules sent by direct message.

    Only when typed into the conversation with one of OWNER_RULE_RECIPIENTS.
    The owner's every message reaches this app, including the ones to
    patients; a rule accepted from any conversation would be a rule the
    patient in it also read. The confirmation goes back into that same
    conversation, which is the owner's own chosen one.
    """
    settings = get_settings()
    tenant_uuid = uuid.UUID(tenant_id)
    rule = parse_rule(message_text, settings.admin_command_keyword)
    if rule is None:
        return

    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            channel = await ChannelRepository(session).get(uuid.UUID(channel_id))
            if channel is None:
                return
            instagram = client or get_instagram_client()
            access_token = decrypt(channel.credentials)
            username = await instagram.fetch_username(access_token=access_token, igsid="me")
            recipient = await instagram.fetch_username(
                access_token=access_token, igsid=recipient_external_id
            )
            if not is_admin(username, settings.admin_usernames) or not is_admin(
                recipient, settings.owner_rule_recipient_usernames
            ):
                logger.warning(
                    "owner_rule_refused",
                    extra={"tenant_id": tenant_id, "username": username, "recipient": recipient},
                )
                return

            confirmation = await handle_instruction(
                session,
                tenant_id=tenant_uuid,
                instruction=rule,
                provider=llm_provider or get_llm_provider(),
                embedding_provider=embedding_provider,
            )
            await session.commit()
            await send_reply(
                session,
                channel_id=channel.id,
                recipient_external_id=recipient_external_id,
                text=confirmation,
                last_user_message_at=datetime.now(UTC),
                adapter=adapter,
            )
            logger.info(
                "owner_instruction_handled", extra={"tenant_id": tenant_id, "username": username}
            )
    finally:
        reset_current_tenant(token)


async def resolve_username(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    user_id: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
) -> None:
    """ARQ job: put the patient's Instagram handle on their row, once.

    Its own job rather than a step inside process_inbound_message, for two
    reasons. A patient who writes while the assistant is switched off never
    reaches that job at all -- and those are exactly the conversations an
    operator is about to answer by hand, so they are the ones that most need
    a name on them. And a lookup against Meta has no business sitting on the
    path that answers a patient: it is slower than the reply is allowed to
    be, and it is allowed to fail, which the reply is not.

    Enqueued on every inbound message and cheap on all but the first: a
    patient who already has a handle is one SELECT and nothing else.
    """
    token = set_current_tenant(uuid.UUID(tenant_id))
    try:
        async with session_factory() as session:
            resolved = await ensure_instagram_username(
                session,
                channel_id=uuid.UUID(channel_id),
                user_id=uuid.UUID(user_id),
            )
            if resolved is not None:
                await session.commit()
    finally:
        reset_current_tenant(token)


# The worker is a separate process from the web app, so it needs its own
# handler installed -- app.main's call never runs here.
configure_logging()


# An automatic reply lands within seconds of the message it answers and says
# the same words to everybody; a person typing a saved reply is slower.
_AUTO_REPLY_SECONDS = 15
_AUTO_REPLY_SEEN_IN = 2


async def _is_automatic_reply(
    session: AsyncSession, conversation_id: uuid.UUID, text: str | None, sent_at: str | None
) -> bool:
    if sent_at is None:
        return False
    try:
        sent = datetime.fromisoformat(sent_at)
    except ValueError:
        return False
    # The patient's message this echo answered: the last one before it, not
    # one they sent in the twenty seconds this job waited.
    patient_wrote = await session.scalar(
        select(sql.max(Message.created_at)).where(
            Message.conversation_id == conversation_id,
            Message.sender == MessageSender.PATIENT,
            Message.created_at <= sent,
        )
    )
    if patient_wrote is None or (sent - patient_wrote).total_seconds() > _AUTO_REPLY_SECONDS:
        return False
    if not (text or "").strip():
        # Something with no words, seconds after the patient wrote: the part
        # of an automatic reply that is not text. A person does not answer
        # with a sticker or a voice note that fast (live, 2026-10-17: the
        # assistant switched off after "Murojaat uchun: ...", with nothing
        # else from the clinic in the transcript).
        return True
    elsewhere = await session.scalar(
        select(sql.count(sql.distinct(Message.conversation_id))).where(
            Message.sender == MessageSender.OPERATOR,
            Message.content == text,
            Message.conversation_id != conversation_id,
            Message.created_at >= datetime.now(UTC) - timedelta(days=30),
        )
    )
    return (elsewhere or 0) >= _AUTO_REPLY_SEEN_IN


async def note_clinic_reply(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    patient_external_id: str,
    text: str | None,
    *,
    sent_at: str | None = None,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
) -> None:
    """ARQ job: the clinic's account sent this patient a message; if a person
    typed it in the Instagram app, that person has the conversation now.

    Not the account's automatic reply, though: the same "Murojaat uchun:
    ☎️..." a few seconds after every "Siz bilan qanday bog'lansam bo'ladi?"
    is Instagram answering, not somebody taking the conversation, and it was
    switching the assistant off for every patient who tapped that button
    (live, 2026-10-16). It is written into the transcript and nothing more.

    Instagram echoes every message the account sends, the assistant's too,
    with nothing that says who sent it. Run a little after the echo, so the
    assistant's own send has been recorded by then: an echo whose text is one
    of the messages recorded in the last few minutes is ours, and anything
    else -- including a photo, which the assistant never sends -- a person's.

    A person's message unpins the conversation, is written into the
    transcript so the dashboard shows what was said, and takes the assistant
    off this conversation: the doctor asked that the bot stop by itself the
    moment somebody from the clinic writes, rather than talk over them. It
    comes back with "Botga qaytarish" on the dashboard.
    """
    token = set_current_tenant(uuid.UUID(tenant_id))
    try:
        async with session_factory() as session:
            user = await UserRepository(session).get_by_external_id(
                channel_id=uuid.UUID(channel_id), external_id=patient_external_id
            )
            if user is None:
                return
            conversation = await ConversationRepository(session).get_open_for_user(user.id)
            if conversation is None:
                return
            if (text or "").strip():
                since = datetime.now(UTC) - timedelta(minutes=10)
                recent = (
                    await session.execute(
                        select(Message.content).where(
                            Message.conversation_id == conversation.id,
                            Message.sender != MessageSender.PATIENT,
                            Message.created_at >= since,
                        )
                    )
                ).scalars()
                if any(handoff.same_message(text, content) for content in recent):
                    return
            if await _is_automatic_reply(session, conversation.id, text, sent_at):
                if (text or "").strip():
                    await record_outbound_message(
                        session,
                        conversation_id=conversation.id,
                        channel_type="instagram",
                        text=text or "",
                        sender=MessageSender.OPERATOR,
                    )
                    await session.commit()
                logger.info(
                    "clinic_auto_reply_noted", extra={"conversation_id": str(conversation.id)}
                )
                return
            await handoff.clear(session, conversation.id)
            if (text or "").strip():
                await record_outbound_message(
                    session,
                    conversation_id=conversation.id,
                    channel_type="instagram",
                    text=text or "",
                    sender=MessageSender.OPERATOR,
                )
            paused = conversation.is_bot_enabled
            if paused:
                conversation.is_bot_enabled = False
                await session.flush()
            await session.commit()
            logger.info(
                "clinic_replied_in_app",
                extra={
                    "conversation_id": str(conversation.id),
                    "bot_paused": paused,
                    # Enough to tell a person from automation afterwards.
                    "has_text": bool((text or "").strip()),
                    "sent_at": sent_at,
                },
            )
    finally:
        reset_current_tenant(token)


async def mirror_daily_report(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
) -> None:
    """ARQ cron: the dashboard's report, today's and yesterday's line, into
    the clinic's sheet (see app.services.sheet_report). Nothing when no
    sheet is configured."""
    from app.services.sheet_report import mirror_report
    from app.services.sheets import get_mirror

    if get_mirror() is None:
        return
    today = datetime.now(CLINIC_TIMEZONE).date()
    async with session_factory() as session:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars())
        for tid in tenant_ids:
            token = set_current_tenant(tid)
            try:
                await mirror_report(session, tid, [today - timedelta(days=1), today])
            finally:
                reset_current_tenant(token)


async def label_booking_weekdays(ctx: dict[str, Any]) -> None:
    """ARQ cron: the weekday in front of each date in the appointment book."""
    await label_appointment_weekdays()


async def review_tenant(
    ctx: dict[str, Any],
    tenant_id: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    llm_provider: LLMProvider | None = None,
) -> int:
    """Read one clinic's recent conversations and propose fixes.

    Returns how many suggestions were stored. Proposals only: nothing the
    review writes reaches a patient until the clinic accepts it.
    """
    tid = uuid.UUID(tenant_id)
    # One review per clinic at a time: the button pressed during the nightly
    # run would otherwise read the same day twice and propose it twice.
    lock = ctx["redis"].lock(f"review-lock:{tenant_id}", timeout=900) if "redis" in ctx else None
    if lock is not None and not await lock.acquire(blocking=False):
        logger.info("review_tenant_already_running", extra={"tenant_id": tenant_id})
        return 0
    token = set_current_tenant(tid)
    try:
        async with session_factory() as session:
            tenant = await session.get(Tenant, tid)
            if tenant is None:
                return 0
            run = await run_review(
                session,
                tenant,
                since=review_window_start(tenant),
                llm_provider=llm_provider,
            )
        logger.info(
            "review_tenant_done",
            extra={"tenant_id": tenant_id, "examined": run.examined, "suggested": run.created},
        )
        return run.created
    finally:
        reset_current_tenant(token)
        if lock is not None:
            with suppress(Exception):
                await lock.release()


async def review_conversations(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    llm_provider: LLMProvider | None = None,
) -> None:
    """Cron job: the nightly review, clinic by clinic.

    One clinic failing -- a model outage, a bad row -- is logged and the
    next clinic still gets its review.
    """
    async with session_factory() as session:
        tenant_ids = list((await session.execute(select(Tenant.id))).scalars())
    pool = ctx.get("redis")
    if pool is not None:
        # A job per clinic, each with its own timeout and retry, so one
        # clinic's slow night cannot run the others out of time.
        for tid in tenant_ids:
            await pool.enqueue_job("review_tenant", str(tid))
        return
    for tid in tenant_ids:
        try:
            await review_tenant(
                ctx, str(tid), session_factory=session_factory, llm_provider=llm_provider
            )
        except Exception:
            logger.exception("review_tenant_failed", extra={"tenant_id": str(tid)})


async def _start_doctor_telegram(ctx: dict[str, Any]) -> None:
    """Start the doctor's Telegram bot beside the queue, if it is configured."""
    stop = asyncio.Event()
    ctx["doctor_telegram_stop"] = stop
    ctx["doctor_telegram_task"] = asyncio.create_task(doctor_telegram.run_forever(stop))
    # arq's run_at_startup did not fire on this deployment, so the weekday
    # labels are refreshed here directly rather than waiting for :15.
    ctx["weekday_labels_task"] = asyncio.create_task(label_appointment_weekdays())


async def _stop_doctor_telegram(ctx: dict[str, Any]) -> None:
    stop = ctx.get("doctor_telegram_stop")
    task = ctx.get("doctor_telegram_task")
    if stop is not None:
        stop.set()
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def handle_patient_media(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    user_id: str,
    conversation_id: str,
    sender_external_id: str,
    external_id: str | None,
    urls: list[str],
    media_ids: list[str] | None = None,
) -> None:
    """ARQ job: a patient sent photos. Store them, acknowledge, tell the doctor.

    Each step is guarded on its own (see app.services.patient_media): a photo
    that could not be downloaded is still recorded by its link, and a patient
    who could not be answered still has their photo reach the doctor.
    """
    tenant_uuid = uuid.UUID(tenant_id)
    token = set_current_tenant(tenant_uuid)
    try:
        async with db_session() as session:
            media = await patient_media.store(
                session,
                tenant_id=tenant_uuid,
                user_id=uuid.UUID(user_id),
                conversation_id=uuid.UUID(conversation_id),
                external_id=external_id,
                urls=urls,
                channel_id=uuid.UUID(channel_id),
                media_ids=[uuid.UUID(m) for m in media_ids] if media_ids else None,
            )
            # Only a person can look at a photo; the conversation waits on one.
            await handoff.flag(session, uuid.UUID(conversation_id))
            await session.commit()
            # The photo is stored and the doctor told either way; only the
            # acknowledgement is the assistant speaking, and a clinic that has
            # switched it off -- for everyone or for this patient -- hears
            # nothing from it, as with a voice note or a reel.
            conversation = await session.get(Conversation, uuid.UUID(conversation_id))
            muted = not await bot_replies_enabled(session, tenant_uuid) or (
                conversation is not None and not conversation.is_bot_enabled
            )
            if muted:
                await _notify_doctor_of_media(session, tenant_uuid, user_id, media)
                return
            if not await _may_answer(
                session, channel_id=uuid.UUID(channel_id), user_id=uuid.UUID(user_id)
            ):
                return
            try:
                await patient_media.acknowledge(
                    session,
                    ctx["redis"],
                    channel_id=uuid.UUID(channel_id),
                    conversation_id=uuid.UUID(conversation_id),
                    recipient_external_id=sender_external_id,
                )
            except Exception:  # noqa: BLE001 - the doctor must still be told
                await session.rollback()
                logger.exception("patient_media_ack_failed")
            await _notify_doctor_of_media(session, tenant_uuid, user_id, media)
            logger.info(
                "patient_media_handled", extra={"tenant_id": tenant_id, "count": len(media)}
            )
    finally:
        reset_current_tenant(token)


async def store_attachments(
    ctx: dict[str, Any], tenant_id: str, channel_id: str, media_ids: list[str]
) -> None:
    """ARQ job: download a voice message, a video or a file a patient sent,
    so the clinic can still play it after Instagram's link has expired.

    Photos are stored by handle_patient_media, and shared reels when somebody
    first opens one (app.api.admin.media); both are skipped here.
    """
    token = set_current_tenant(uuid.UUID(tenant_id))
    try:
        async with db_session() as session:
            wanted = []
            for media_id in media_ids:
                media = await session.get(PatientMedia, uuid.UUID(media_id))
                if media is not None and media.kind in patient_media.DOWNLOAD_ON_ARRIVAL:
                    wanted.append(media.id)
            if not wanted:
                return
            rows = await patient_media.fill(
                session,
                wanted,
                token=await patient_media.channel_token(session, uuid.UUID(channel_id)),
            )
            logger.info(
                "attachments_stored",
                extra={
                    "tenant_id": tenant_id,
                    "stored": sum(r.size_bytes is not None for r in rows),
                    "of": len(rows),
                },
            )
    finally:
        reset_current_tenant(token)


async def _notify_doctor_of_media(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: str, media: Any
) -> None:
    try:
        patient = await session.get(User, uuid.UUID(user_id))
        await doctor_telegram.notify_patient_media(
            session, tenant_id, media, patient_media.caption(patient, len(media))
        )
    except Exception:  # noqa: BLE001
        logger.exception("patient_media_notify_failed")


async def _said_today(
    session: AsyncSession, conversation_id: uuid.UUID, texts: Iterable[str]
) -> bool:
    """Whether the clinic sent any of these fixed lines here in the last day."""
    since = datetime.now(UTC) - timedelta(hours=24)
    sent = (
        await session.execute(
            select(Message.content).where(
                Message.conversation_id == conversation_id,
                Message.sender != MessageSender.PATIENT,
                Message.created_at >= since,
            )
        )
    ).scalars()
    lines = list(texts)
    return any(handoff.same_message(content, line) for content in sent for line in lines)


async def answer_attachment(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    conversation_id: str,
    sender_external_id: str,
    kind: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    adapter: ChannelAdapter | None = None,
) -> None:
    """ARQ job: anything sent without words -- a video, a file, a shared reel,
    a sticker, a story mention -- answered without the model.

    The model cannot watch a video or open a reel, so anything it wrote about
    one would be about something nobody had seen. The clinic's own answer is
    a greeting and an offer of help, once a day: "Qaysi masala bo'yicha
    yubordingiz?" under somebody's shared reel read as an interrogation.
    A video or a file still waits on the doctor, as a photo does.
    """
    tenant_uuid = uuid.UUID(tenant_id)
    conversation_uuid = uuid.UUID(conversation_id)
    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            for_a_person = kind in (message_labels.VIDEO, message_labels.FILE)
            if for_a_person:
                await handoff.flag(session, conversation_uuid)
                await session.commit()
            if not await bot_replies_enabled(session, tenant_uuid):
                return
            conversation = await session.get(Conversation, conversation_uuid)
            if conversation is None or not conversation.is_bot_enabled:
                return
            if not await _may_answer(
                session, channel_id=uuid.UUID(channel_id), user_id=conversation.user_id
            ):
                return
            if kind == message_labels.REACTION:
                return
            window = await debounce_window(session, tenant_uuid)
            if window is None:
                window = get_settings().debounce_window_seconds
            if await _patient_typed_since(
                session, conversation_uuid, seconds=window + _TYPED_LOOKBACK_PADDING_SECONDS
            ):
                # A reel shared with "Narxi qancha?" under it is a question, and
                # the debounced reply answers it. A greeting on top of that
                # answer is the clinic speaking twice -- the second time as if
                # it had not read what the patient wrote.
                logger.info("attachment_greeting_skipped_patient_wrote")
                return
            if await _said_today(session, conversation_uuid, ATTACHMENT_GREETINGS.values()):
                logger.info("attachment_greeting_skipped_repeat")
                return
            text = ATTACHMENT_GREETINGS[
                await patient_media.patient_script(session, conversation_uuid)
            ]
            delivered = await send_reply(
                session,
                channel_id=uuid.UUID(channel_id),
                recipient_external_id=sender_external_id,
                text=text,
                last_user_message_at=await last_inbound_at(session, conversation_uuid)
                or datetime.now(UTC),
                reply_context=await reply_context_for(session, conversation_uuid),
                adapter=adapter,
            )
            if delivered is not None:
                await record_outbound_message(
                    session, conversation_id=conversation_uuid, channel_type=delivered, text=text
                )
                await session.commit()
            logger.info(
                "attachment_answered", extra={"kind": kind, "delivered": delivered is not None}
            )
    finally:
        reset_current_tenant(token)


# How far back answer_attachment looks for words the patient typed, beyond
# the debounce window itself: the job runs a little after the window closes,
# and a question typed just before the reel counts as much as one after it.
_TYPED_LOOKBACK_PADDING_SECONDS = 30


async def _patient_typed_since(
    session: AsyncSession, conversation_id: uuid.UUID, *, seconds: int
) -> bool:
    """Whether the patient wrote any words of their own in the last `seconds`
    -- not a label the system recorded for a photo, reel or sticker."""
    since = datetime.now(UTC) - timedelta(seconds=seconds)
    written = (
        await session.execute(
            select(Message.content).where(
                Message.conversation_id == conversation_id,
                Message.sender == MessageSender.PATIENT,
                Message.created_at >= since,
            )
        )
    ).scalars()
    return any(not message_labels.is_label(content) for content in written)


# What the clinic says to anything sent without words: the assistant cannot
# open it, so it greets the patient and invites the question -- the way an
# operator would, plainly: no "bemalol", no "how may I help you" (the owner,
# 2026-10-16: write like operators talk, without courtesies).
ATTACHMENT_GREETINGS = {
    "uz-latn": "Assalomu alaykum. Savolingiz bo'lsa yozing.",
    "uz-cyrl": "Ассалому алайкум. Саволингиз бўлса ёзинг.",
    "ru": "Здравствуйте. Если есть вопрос, напишите.",
}


async def answer_voice_note(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    conversation_id: str,
    sender_external_id: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    adapter: ChannelAdapter | None = None,
) -> None:
    """ARQ job: answer a voice message with the clinic's one fixed line.

    Not the model: it cannot hear the recording, so any reply it wrote would
    be about a message nobody had listened to. The patient is told plainly
    that the doctor answers these.
    """
    tenant_uuid = uuid.UUID(tenant_id)
    conversation_uuid = uuid.UUID(conversation_id)
    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            # Only a person can listen to it; the conversation waits on one,
            # whether or not the assistant says so below.
            await handoff.flag(session, conversation_uuid)
            await session.commit()
            if not await bot_replies_enabled(session, tenant_uuid):
                return
            conversation = await session.get(Conversation, conversation_uuid)
            # Staff who have taken the conversation over hear the recording
            # themselves; the bot says nothing, as with a photo or a reel.
            if conversation is not None and not conversation.is_bot_enabled:
                return
            if conversation is not None and not await _may_answer(
                session, channel_id=uuid.UUID(channel_id), user_id=conversation.user_id
            ):
                return
            text = await voice_notes.reply_for(session, conversation_uuid)
            if await _said_today(session, conversation_uuid, voice_notes.REPLIES.values()):
                # The patient has been told once today that the doctor answers
                # these; telling them under every recording is a wall of it.
                logger.info("voice_note_reply_skipped_repeat")
                return
            delivered = await send_reply(
                session,
                channel_id=uuid.UUID(channel_id),
                recipient_external_id=sender_external_id,
                text=text,
                last_user_message_at=await last_inbound_at(session, conversation_uuid)
                or datetime.now(UTC),
                reply_context=await reply_context_for(session, conversation_uuid),
                adapter=adapter,
            )
            if delivered is not None:
                await record_outbound_message(
                    session,
                    conversation_id=conversation_uuid,
                    channel_type=delivered,
                    text=text,
                )
                await session.commit()
            logger.info("voice_note_answered", extra={"delivered": delivered is not None})
    finally:
        reset_current_tenant(token)


async def answer_comment(
    ctx: dict[str, Any],
    tenant_id: str,
    channel_id: str,
    comment_id: str,
    author_external_id: str | None,
    username: str | None,
    text: str,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = db_session,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    client: InstagramClient | None = None,
    media_id: str | None = None,
) -> None:
    """ARQ job: answer a comment in Direct, and say so under the comment.

    The real answer goes to the writer's inbox -- a comment thread is a
    public place, and a patient's question is not. The public reply is one
    line telling them (and everyone reading) that it has been answered there.
    A comment of only emoji gets a public thank-you and nothing in Direct.
    """
    settings = get_settings()
    allowed = settings.comment_reply_usernames or settings.answer_only_usernames
    if allowed and not is_admin(username, allowed):
        logger.info("comment_ignored_not_on_list", extra={"username": username})
        return

    tenant_uuid = uuid.UUID(tenant_id)
    token = set_current_tenant(tenant_uuid)
    try:
        async with session_factory() as session:
            if not await bot_replies_enabled(session, tenant_uuid):
                return
            channel = await ChannelRepository(session).get(uuid.UUID(channel_id))
            if channel is None:
                return
            access_token = decrypt(channel.credentials)
            instagram = client or get_instagram_client()

            if not await comment_replies_enabled(session, tenant_uuid):
                # Comments switched off: kept where staff will see them, under
                # "Izohlar", and nothing posted or sent.
                if author_external_id and comments.has_words(text):
                    kept = await register_inbound_message(
                        session,
                        channel_id=uuid.UUID(channel_id),
                        channel_type=str(channel.type),
                        sender_external_id=author_external_id,
                        text=f"{message_labels.COMMENT_PREFIX}{text}",
                        reply_context={"post_media_id": media_id} if media_id else None,
                    )
                    # Fetched even when not answered: the dashboard shows
                    # staff which video the comment was under.
                    await instagram_posts.context(
                        session,
                        tenant_id=tenant_uuid,
                        client=instagram,
                        access_token=access_token,
                        media_id=media_id,
                        llm_provider=llm_provider,
                    )
                    author = await session.get(User, kept.user_id)
                    if author is not None and username and not author.username:
                        author.username = username.lstrip("@")
                    await session.commit()
                logger.info("comment_replies_off", extra={"username": username})
                return

            if not comments.has_words(text):
                with suppress(Exception):
                    await instagram.reply_to_comment(
                        access_token=access_token,
                        comment_id=comment_id,
                        text=comments.thanks_reply(),
                    )
                logger.info("comment_thanked", extra={"username": username})
                return

            # The post the comment is about: fetched, its video heard and
            # summarised the first time, read back after that.
            post = await instagram_posts.context(
                session,
                tenant_id=tenant_uuid,
                client=instagram,
                access_token=access_token,
                media_id=media_id,
                llm_provider=llm_provider,
            )
            caption = post.caption if post is not None else None

            # Recorded as a message from this patient, so the conversation the
            # reply lands in reads in order: what they asked, then the answer.
            # The post's id rides in the message's meta, so the dashboard can
            # show which video the comment was under.
            inbound = None
            if author_external_id:
                inbound = await register_inbound_message(
                    session,
                    channel_id=uuid.UUID(channel_id),
                    channel_type=str(channel.type),
                    sender_external_id=author_external_id,
                    text=f"{message_labels.COMMENT_PREFIX}{text}",
                    reply_context={"post_media_id": media_id} if media_id else None,
                )
                # The comment arrives with the author's handle, and nothing
                # else will ever tell us it: without it the dashboard lists
                # them by Instagram's 16-digit id (seen 2026-10-16).
                author = await session.get(User, inbound.user_id)
                if author is not None and username and not author.username:
                    author.username = username.lstrip("@")
                await session.commit()

            reply = await generate_answer(
                session,
                text,
                embedding_provider=embedding_provider,
                llm_provider=llm_provider or get_comment_llm_provider(),
                history=(
                    await context_for_reply(session, inbound.conversation_id)
                    if inbound is not None
                    else None
                ),
                situation=comments.situation(caption, instagram_posts.describe(post)),
            )
            reply, _ = callbacks.extract(reply)
            reply, needs_doctor = handoff.extract(reply)
            # No offer to book under a comment: the doctor's rule, and the
            # model still made one there (live, 2026-10-17).
            reply = booking_offer.strip(reply) or reply
            if needs_doctor and inbound is not None:
                await handoff.flag(session, inbound.conversation_id)
                await session.commit()

            answered_in_direct = False
            try:
                await instagram.send_private_reply(
                    access_token=access_token, comment_id=comment_id, text=reply
                )
                answered_in_direct = True
            except Exception:  # noqa: BLE001 - the public line must still go out
                logger.exception("comment_private_reply_failed")
            else:
                if inbound is not None:
                    await record_outbound_message(
                        session,
                        conversation_id=inbound.conversation_id,
                        channel_type=str(channel.type),
                        text=reply,
                    )
                    await session.commit()

            with suppress(Exception):
                await instagram.reply_to_comment(
                    access_token=access_token,
                    comment_id=comment_id,
                    text=comments.public_reply(text, answered_in_direct=answered_in_direct),
                )
            logger.info("comment_answered", extra={"username": username})
    finally:
        reset_current_tenant(token)


class WorkerSettings:
    on_startup = _start_doctor_telegram
    on_shutdown = _stop_doctor_telegram
    functions = [
        process_inbound_message,
        fire_debounce_window,
        resolve_username,
        apply_admin_rule,
        apply_owner_rule,
        handle_patient_media,
        store_attachments,
        answer_voice_note,
        answer_comment,
        note_clinic_reply,
        answer_attachment,
        # A few calls to a model thinking hard; arq's five minutes is too few.
        func(review_tenant, timeout=900, max_tries=1),
    ]
    # Named here as well as in _MAX_ATTEMPTS so the retry ladder in
    # fire_debounce_window and arq's own limit cannot drift apart.
    max_tries = _MAX_ATTEMPTS
    # Every five minutes. The reminder windows are hours wide and the job
    # catches up on anything it missed, so this is about how promptly a
    # reminder lands inside its window rather than about not losing one.
    cron_jobs = [
        cron(send_appointment_reminders, minute=set(range(0, 60, 5))),
        # 03:20 rather than on the hour: nothing else the clinic depends
        # on runs then, and Meta is quietest.
        cron(refresh_channel_tokens, hour={3}, minute={20}),
        # Every ten minutes. A cleared webhook is a total outage on that
        # channel, so the interval is really the worst case a patient
        # waits before the bot can hear them again.
        cron(verify_channel_webhooks, minute=set(range(0, 60, 10))),
        # Hourly, so a date someone changed by hand catches up. The startup
        # refresh is in _start_doctor_telegram.
        cron(label_booking_weekdays, minute={15}),
        # Hourly: today's report line fills in as the day goes, and
        # yesterday's gets its last hours once more after midnight.
        cron(mirror_daily_report, minute={25}),
        # 02:30 in Tashkent: the day's conversations are over and the
        # suggestions are waiting when the clinic opens.
        cron(review_conversations, hour={21}, minute={30}),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
