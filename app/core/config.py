from functools import lru_cache
from typing import Literal, Self

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(alias="DATABASE_URL")
    # Set on a shared Postgres server: the only database name this deployment
    # may use (see _require_the_expected_database).
    required_database_name: str | None = Field(default=None, alias="REQUIRED_DATABASE_NAME")
    redis_url: str = Field(alias="REDIS_URL")
    webhook_verify_token: str = Field(alias="WEBHOOK_VERIFY_TOKEN")
    meta_app_secret: str = Field(alias="META_APP_SECRET")
    # Symmetric key for app.core.encryption (channel.credentials at rest —
    # access tokens, not just placeholders, live there). Required, not
    # optional-with-a-plaintext-fallback: a missing or malformed key must
    # fail app startup loudly rather than let a secret get written in
    # plaintext by accident. Generate with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    encryption_key: str = Field(alias="ENCRYPTION_KEY")
    # Signs the operator dashboard's session cookie (app.core.session) —
    # separate from encryption_key since it protects a different thing
    # (tamper-evidence on a cookie, not confidentiality of stored data) and
    # rotating one shouldn't force rotating the other. Generate with
    # `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    session_secret_key: str = Field(alias="SESSION_SECRET_KEY")
    # False by default so login works over plain http://localhost in local
    # dev — a hardcoded Secure flag would make the browser silently refuse
    # to store the cookie there. Set true explicitly in production (which
    # must be behind HTTPS), rather than inferring it from the request's
    # scheme: inference only works if the deployment correctly forwards
    # X-Forwarded-Proto and uvicorn is run with --proxy-headers, and a
    # misconfiguration there would silently downgrade to an insecure cookie.
    # A config flag fails safe — wrong by default (False, i.e. explicit
    # opt-in) rather than wrong by a proxy-setup mistake.
    session_cookie_secure: bool = Field(default=False, alias="SESSION_COOKIE_SECURE")
    # Meta signs every webhook delivery, and app.api.webhook rejects a
    # mismatch with 403. Setting this false downgrades that rejection to a
    # log line: the check still runs and still reports what it saw, but a
    # request that fails it is processed anyway. That is a deliberate hole
    # in the only evidence a webhook actually came from Meta, so it exists
    # to diagnose a secret mismatch against live traffic -- not as a
    # resting state. Defaults to enforcing, so the hole is only ever opened
    # by someone explicitly setting the variable.
    webhook_signature_enforced: bool = Field(default=True, alias="WEBHOOK_SIGNATURE_ENFORCED")

    # Whether /docs, /redoc and /openapi.json are served. They need no
    # authentication and describe every route and payload the API has, which
    # is a map for anyone looking for a way in. Off by default: a deployment
    # that wants them is developing against it and can say so.
    api_docs_enabled: bool = Field(default=False, alias="API_DOCS_ENABLED")

    # The language to reply in when the patient's own is unclear. Patients
    # open with "Salom", "Alik", "Nmagap" -- too short and too transliterated
    # for a model to place, and it falls back to English, which reads as the
    # wrong clinic answering. Named in English ("Uzbek", "Russian") because
    # it goes into an English system prompt. Uzbek by default: an English
    # default is what answered "Nmagap" with "Hi. What do you need help with?".
    default_reply_language: str = Field(
        default="Uzbek, in the Latin alphabet", alias="DEFAULT_REPLY_LANGUAGE"
    )

    # The clinic's street address, given to the model as a fact. Free-form,
    # including any landmark worth reading back.
    clinic_address: str | None = Field(default=None, alias="CLINIC_ADDRESS")

    # The clinic's own reception numbers, given to the model as a fact.
    # Free-form, because "+998 90 123 45 67 or +998 71 200 00 00" is what a
    # clinic actually wants read back.
    clinic_phone_numbers: str | None = Field(default=None, alias="CLINIC_PHONE_NUMBERS")

    # When the clinic is open, as one sentence in the clinic's own words --
    # "Dushanbadan shanbagacha 09:00 dan 18:00 gacha", say.
    clinic_work_hours: str | None = Field(default=None, alias="CLINIC_WORK_HOURS")

    # What the sign-in page calls the product. It is shown before anybody is
    # logged in, so it is not read from a doctor's account: one sign-in page
    # serves every deployment, and a deployment that wants the doctor's name
    # there sets it.
    product_name: str = Field(default="medai", alias="PRODUCT_NAME")

    # Whose inbox this is, when the deployment answers one doctor's own
    # Instagram rather than the clinic's -- the doctor's full name and
    # "urolog-androlog", say. Both set switches the assistant from the
    # clinic's front desk to the person who runs that doctor's inbox (see
    # app.services.answer._opening). Unset keeps the clinic voice.
    doctor_name: str | None = Field(default=None, alias="DOCTOR_NAME")
    doctor_specialty: str | None = Field(default=None, alias="DOCTOR_SPECIALTY")

    # The doctor's experience and training, given to the model as a fact
    # (see app.services.answer._DOCTOR_BACKGROUND). One line per fact,
    # separated by " | " because an environment variable is one line.
    doctor_background: str | None = Field(default=None, alias="DOCTOR_BACKGROUND")

    # Whether the assistant books appointments itself -- asking the patient
    # for their name, number and complaint, offering free slots, and writing
    # the booking -- instead of sending every patient to the telephone.
    booking_enabled: bool = Field(default=False, alias="BOOKING_ENABLED")

    # While testing on the live account: answer only these Instagram handles
    # (comma-separated, "@" optional). Everybody else is recorded but not
    # answered. Empty -- the default -- answers everyone.
    answer_only_usernames_raw: str = Field(default="", alias="ANSWER_ONLY_USERNAMES")

    # Whose comments under the account's posts get an answer. Empty falls
    # back to ANSWER_ONLY_USERNAMES, and if that is empty too, every comment
    # is answered.
    comment_reply_usernames_raw: str = Field(default="", alias="COMMENT_REPLY_USERNAMES")

    @property
    def answer_only_usernames(self) -> list[str]:
        return [n.strip() for n in self.answer_only_usernames_raw.split(",") if n.strip()]

    @property
    def comment_reply_usernames(self) -> list[str]:
        return [n.strip() for n in self.comment_reply_usernames_raw.split(",") if n.strip()]

    @property
    def doctor_background_text(self) -> str | None:
        if not self.doctor_background:
            return None
        lines = [line.strip() for line in self.doctor_background.split("|") if line.strip()]
        return "\n".join(f"- {line}" for line in lines) or None

    # Instagram handles allowed to set the clinic's standing rules by direct
    # message (see app.services.admin_commands). Comma-separated, "@" optional.
    #
    # Empty -- the default -- means nobody can, which is the right default for
    # a feature whose whole surface is "somebody messages the clinic". A
    # deployment that wants it names the handles, and a handle that changes
    # hands is removed here rather than discovered later.
    admin_instagram_usernames: str = Field(default="", alias="ADMIN_INSTAGRAM_USERNAMES")

    # What an admin types in front of a rule. Configurable so a deployment can
    # change it without shipping code -- but it is not a secret and must not
    # be treated as one: it travels in plain text through Instagram, and this
    # file's default is public. The handle list above is the actual gate.
    admin_command_keyword: str = Field(default="Aiadm1in:", alias="ADMIN_COMMAND_KEYWORD")

    # Where the account's owner may type a rule from their own account (see
    # app.workers.tasks.apply_owner_rule). Every message the owner sends
    # reaches this app, so without a narrow place to send rules to, a keyword
    # typed into a patient's chat would both set a rule and show it to that
    # patient. Comma-separated handles, "@" optional. Empty means nowhere:
    # owner rules are off.
    owner_rule_recipients: str = Field(default="", alias="OWNER_RULE_RECIPIENTS")

    # The doctor's own Telegram bot (app.services.doctor_telegram): new
    # appointments, the day's list, and a day off in one message. The token
    # is from @BotFather; unset leaves the bot off. The usernames are the
    # Telegram handles allowed to use it -- anybody else who finds the bot is
    # told it is not for them, since it lists patients by name and number.
    doctor_telegram_bot_token: str | None = Field(default=None, alias="DOCTOR_TELEGRAM_BOT_TOKEN")
    doctor_telegram_usernames: str = Field(default="", alias="DOCTOR_TELEGRAM_USERNAMES")
    # Handles (or numeric Telegram ids) that may look but not cancel: an
    # assistant who answers the phone, not the person who closes the day.
    doctor_telegram_viewers: str = Field(default="", alias="DOCTOR_TELEGRAM_VIEWERS")

    @property
    def owner_rule_recipient_usernames(self) -> list[str]:
        return [name.strip() for name in self.owner_rule_recipients.split(",") if name.strip()]

    @property
    def admin_usernames(self) -> list[str]:
        return [name.strip() for name in self.admin_instagram_usernames.split(",") if name.strip()]

    # How long to wait for a patient to finish typing before answering. The
    # wait exists so a question split across bubbles ("Salom" / "narxi
    # qancha?") gets one answer instead of one per bubble -- but it is dead
    # time the patient spends staring at a silent chat, so it is deliberately
    # short: long enough to catch the next bubble of a burst, not long enough
    # to read as the bot ignoring them. 0 disables batching entirely and
    # answers every bubble on its own.
    #
    # TODO(IGB-?): move onto Tenant/per-tenant settings once the admin panel
    # (TZ 4.2) exists, so each clinic can tune its own debounce window
    # instead of every tenant sharing this one value — same pattern as
    # the other per-deployment settings here.
    debounce_window_seconds: int = Field(default=5, ge=0, alias="DEBOUNCE_WINDOW_SECONDS")

    # The Instagram access token this deployment's channel should carry. Read
    # here only so first-run provisioning (app.core.provisioning) can seed a
    # channel with a usable credential; the running pipeline reads the token
    # from channel.credentials, never from configuration.
    access_token: str | None = Field(default=None, alias="ACCESS_TOKEN")
    # Set both to have web startup create the tenant/channel this deployment
    # serves; see app.core.provisioning for why that lives in the app. Unset
    # (the default) skips provisioning entirely, and they can be removed once
    # the rows exist.
    provision_tenant_name: str | None = Field(default=None, alias="PROVISION_TENANT_NAME")
    provision_ig_account_id: str | None = Field(default=None, alias="PROVISION_IG_ACCOUNT_ID")

    # This deployment's own public origin, e.g.
    # "https://clinic-bot.up.railway.app". Used only to tell Telegram where
    # to deliver updates; the app never calls itself. On a managed host it is
    # the one fact the container cannot work out for itself -- the hostname
    # inside the cluster is not the one the outside world reaches it by.
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")

    # The clinic's Telegram bot token (@BotFather). Read only by first-run
    # provisioning, exactly like access_token above: once the channel row
    # exists the running pipeline reads the token from it, encrypted, and
    # never from configuration. Requires public_base_url, since registering
    # the webhook is half of what makes the bot reachable.
    provision_telegram_bot_token: str | None = Field(
        default=None, alias="PROVISION_TELEGRAM_BOT_TOKEN"
    )

    # The first dashboard login. There is no sign-up page and no admin UI for
    # creating accounts, so without this a deployment on a host whose
    # database is only reachable from inside the cluster has no way in at
    # all -- scripts/create_operator.py needs a shell that host does not
    # offer. Same posture as the values above: an instruction to provision,
    # removable once the row exists. The password is only ever used to
    # compute a bcrypt hash; it is never stored or logged.
    provision_operator_username: str | None = Field(
        default=None, alias="PROVISION_OPERATOR_USERNAME"
    )
    provision_operator_password: str | None = Field(
        default=None, alias="PROVISION_OPERATOR_PASSWORD"
    )
    provision_operator_name: str = Field(default="Administrator", alias="PROVISION_OPERATOR_NAME")
    # "operator" (view + book/cancel) rather than "doctor" (view-only): the
    # first account has to be able to actually run the clinic's day, and it
    # is the only account that exists until it creates others.
    provision_operator_role: Literal["admin", "operator", "doctor"] = Field(
        default="admin", alias="PROVISION_OPERATOR_ROLE"
    )

    # Path to a FAQ JSON file to load into knowledge_base on web startup (see
    # app.core.faq_seeding). Like the provisioning values above, it is an
    # instruction to seed rather than a description of the running system --
    # set it for the deploy that loads the file, then unset it. Unset (the
    # default) skips seeding entirely.
    seed_faqs_from: str | None = Field(default=None, alias="SEED_FAQS_FROM")

    # Path to a roster JSON file to load into doctors on web startup (see
    # app.core.doctor_seeding), armed and unset the same way. The roster is
    # what lets the assistant say who works here, and what decides how many
    # bookings one appointment slot holds.
    seed_doctors_from: str | None = Field(default=None, alias="SEED_DOCTORS_FROM")

    # Which OpenAI model answers. Configurable, and named with a concrete
    # version rather than an alias: a model that retires, prices that change,
    # or a reply quality problem are all things to fix from the dashboard
    # while patients are waiting, not things to ship code for -- and an alias
    # can move the model under a deployment with no change on our side, which
    # makes yesterday's behaviour impossible to reproduce.
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")
    # How long a reasoning model thinks before it answers. A patient in a chat
    # waits for every second of it: on gpt-5-mini the default took 7-12 s per
    # reply and "minimal" about 2 s for the same prompt. Ignored by models
    # without reasoning.
    #
    # "minimal" was the setting, and two seconds bought sentences no Uzbek
    # speaker would write: "Эртага 10:30 бурулмаган", "Тасниф: диабетни...",
    # "11:00 band -- mavjud bo'lmagan vaqt" in the same breath. "low" costs
    # about two seconds more and buys back the grammar; the clinic asked for
    # five-second replies, and four is still four.
    openai_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = Field(
        default="low", alias="OPENAI_REASONING_EFFORT"
    )
    # The nightly review of the day's conversations (app.services.review).
    # Nobody waits on it, so it may use a stronger model and think longer
    # than a reply can; unset, it uses OPENAI_MODEL.
    openai_review_model: str | None = Field(default=None, alias="OPENAI_REVIEW_MODEL")

    # Which backend writes the replies. Unset -- the normal case -- they are
    # written by OpenAI, which is also what makes the embeddings.
    #
    # It exists for the one emergency worth an escape hatch: replies can be
    # moved to another backend without touching the embeddings. Swapping the
    # model that writes a reply costs nothing, since the next message is
    # simply written by something else. Swapping the model that makes
    # embeddings invalidates every vector in knowledge_base -- they are only
    # comparable to vectors from the same model -- so it means re-embedding
    # the clinic's whole FAQ before retrieval finds anything again. A
    # deployment locked out of OpenAI at two in the afternoon needs the cheap
    # half of that immediately, and should not have to take the expensive
    # half with it.
    llm_provider: Literal["openai", "qwen"] | None = Field(default=None, alias="LLM_PROVIDER")

    # Hugging Face access token. Its Inference Providers router is
    # OpenAI-compatible, which is what lets Qwen be reached through the same
    # client as OpenAI rather than through a third SDK.
    hf_token: str | None = Field(default=None, alias="HF_TOKEN")

    # --- the clinic's own spreadsheet ---------------------------------------
    #
    # Clinic owners do not open dashboards. They open the sheet they already
    # keep, so the leads have to arrive there too. This is a mirror, never a
    # source: nothing is ever read back, and losing the sheet loses nothing
    # the database does not still hold.
    #
    # The whole service-account JSON, as one value. It has embedded newlines
    # inside private_key, which a KEY=value file cannot carry — so it is
    # accepted either verbatim or base64-encoded, whichever the host makes
    # easier to paste.
    google_service_account_json: str | None = Field(
        default=None, alias="GOOGLE_SERVICE_ACCOUNT_JSON"
    )
    # The id out of the sheet's URL: docs.google.com/spreadsheets/d/<id>/edit.
    # Unset, the whole mirror is off and nothing is attempted.
    google_sheets_spreadsheet_id: str | None = Field(
        default=None, alias="GOOGLE_SHEETS_SPREADSHEET_ID"
    )
    qwen_model: str = Field(default="Qwen/Qwen3-235B-A22B-Instruct-2507", alias="QWEN_MODEL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")

    @field_validator(
        "provision_ig_account_id",
        "provision_tenant_name",
        "clinic_phone_numbers",
        "clinic_address",
        "clinic_work_hours",
        "product_name",
        "doctor_name",
        "doctor_specialty",
        "doctor_background",
        "admin_instagram_usernames",
        "admin_command_keyword",
        "owner_rule_recipients",
        "doctor_telegram_bot_token",
        "doctor_telegram_usernames",
        "doctor_telegram_viewers",
        "seed_faqs_from",
        "seed_doctors_from",
        "public_base_url",
        "provision_telegram_bot_token",
        "provision_operator_username",
        "provision_operator_password",
        "openai_review_model",
        mode="after",
    )
    @classmethod
    def _strip_pasted_values(cls, value: str | None) -> str | None:
        """These are the ones an operator pastes into a hosting dashboard,
        where a trailing newline rides along invisibly and is then stored
        verbatim.

        For the provisioning ids that is silent and total: the newline lands
        in Channel.external_id, resolve_instagram_channel can never match
        it again, and provisioning logs success on one side while every
        webhook logs webhook_unknown_ig_account on the other. For
        clinic_phone_numbers and clinic_address it is merely visible -- the
        assistant reads the value back to a patient with a line break in the
        middle of it.

        The bot token and the bootstrap password are stripped for the same
        reason and are the two worth naming: a stray newline makes getMe
        return Unauthorized, and makes the one password that can log in
        differ from the one its owner typed -- both of which read as "the
        credential is wrong" rather than "the paste picked up a newline".
        """
        return value.strip() if value is not None else None

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_database_driver(cls, value: str) -> str:
        """Rewrite a driverless Postgres URL onto the asyncpg driver.

        app.core.db builds an *async* engine, which needs an explicit
        async driver in the URL — SQLAlchemy maps a bare `postgresql://`
        onto psycopg2, which isn't a dependency, so the app would die at
        startup with NoSuchModuleError. Managed hosts (Railway, Heroku,
        Fly) inject `postgresql://` (or the older `postgres://` alias) and
        offer no way to change it, so accepting their value and fixing the
        scheme here beats hand-assembling the URL from five separate
        credential variables at every deploy — one mistyped part of which
        fails in a way that looks nothing like a typo.

        Anything already carrying a driver (`postgresql+asyncpg://`, or a
        deliberate `postgresql+psycopg://`) is passed through untouched.
        """
        for scheme in ("postgresql://", "postgres://"):
            if value.startswith(scheme):
                return f"postgresql+asyncpg://{value.removeprefix(scheme)}"
        return value

    @model_validator(mode="after")
    def _require_active_provider_key(self) -> Self:
        # OPENAI_API_KEY is not optional any more: it makes every embedding
        # in the system, so a deployment without one cannot retrieve a single
        # FAQ row no matter what writes its replies.
        #
        # Checked at startup rather than on the first patient message: a
        # deployment that names a provider it has no credential for should
        # refuse to boot, not accept webhooks and then fail to answer every
        # one of them.
        if self.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required")
        if self.llm_provider == "qwen" and self.hf_token is None:
            raise ValueError("HF_TOKEN is required when LLM_PROVIDER=qwen")
        return self

    @model_validator(mode="after")
    def _require_the_expected_database(self) -> Self:
        # On a Postgres server shared with another project, the database name
        # in DATABASE_URL is the only thing keeping the two apart. A variable
        # pasted into the wrong service would point this bot at the other
        # project's patients -- so when REQUIRED_DATABASE_NAME is set, any
        # other database refuses to boot rather than quietly mixing rows.
        if self.required_database_name:
            actual = self.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
            if actual != self.required_database_name:
                raise ValueError(
                    f"DATABASE_URL points at database {actual!r}, but this deployment "
                    f"must use {self.required_database_name!r}"
                )
        return self

    @model_validator(mode="after")
    def _require_valid_encryption_key(self) -> Self:
        # Fernet() itself validates: url-safe-base64-decodable and exactly 32
        # bytes once decoded. Both failure modes raise ValueError (binascii's
        # decode error is a ValueError subclass) — re-raised with a message
        # that says what to do about it, not just that construction failed.
        try:
            Fernet(self.encryption_key.encode("utf-8"))
        except ValueError as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a valid Fernet key (32 url-safe "
                "base64-encoded bytes) — generate one with Fernet.generate_key()"
            ) from exc
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
