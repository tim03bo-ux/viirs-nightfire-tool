"""
status.py — permit lifecycle, program and action classification.

An issued permit and an application still sitting in technical review are both
worth tracking, but they mean very different things: one is a project that can
be built, the other is a project someone *wants* to build. Every feed states
that distinction differently ("Issued", "Pending - Public Notice", "IA Signed",
"Study in progress", "Active"), so all of it is collapsed here into one axis.

Three independent questions:

    lifecycle       where the authorization stands: pending / approved /
                    operating / withdrawn / denied / expired
    stage           the finer step within that, where the feed says
                    (technical review, public notice, screening study, ...)
    program         which authorization this is — TCEQ air NSR, PSD, permit by
                    rule, standard permit, Title V, stormwater CGP, ERCOT
                    interconnection
    action          new / amendment / alteration / renewal / registration / ...

The original text is always preserved alongside, because these mappings are
lossy and a reader checking an unexpected result needs to see what the agency
actually wrote.
"""

from datetime import date, datetime

from .normalize import norm_text

# --- Lifecycle ---------------------------------------------------------------

PENDING = "pending"
APPROVED = "approved"
OPERATING = "operating"
WITHDRAWN = "withdrawn"
DENIED = "denied"
EXPIRED = "expired"
UNKNOWN = "unknown"

LIFECYCLE_ORDER = [PENDING, APPROVED, OPERATING, WITHDRAWN, DENIED, EXPIRED, UNKNOWN]

# Lifecycles that mean "this authorization is still being decided".
IN_PROCESS = {PENDING}
# Lifecycles that mean "this project has an authorization it can rely on".
AUTHORIZED = {APPROVED, OPERATING}
# Lifecycles where the project is no longer moving forward.
CLOSED = {WITHDRAWN, DENIED, EXPIRED}

# Checked in this order — a status like "Pending - Public Notice" or "Approved
# for energization" contains words from more than one bucket, and the earlier
# entry wins. Terminal outcomes are checked before in-progress ones so that
# "withdrawn after technical review" is not read as pending.
_LIFECYCLE_PHRASES = [
    (DENIED, [
        "denied", "denial", "rejected", "disapproved", "not granted",
    ]),
    (WITHDRAWN, [
        "withdrawn", "withdrawal", "voided", "void", "returned", "cancelled",
        "canceled", "closed without action", "no action", "abandoned",
        "inactive", "suspended",
    ]),
    (EXPIRED, [
        "expired", "expiration", "terminated", "termination", "revoked",
        "revocation", "surrendered", "notice of termination",
    ]),
    (PENDING, [
        "pending", "under review", "in review", "in process", "in progress",
        "under technical review", "technical review", "administrative review",
        "completeness review", "public notice", "public comment",
        "comment period", "notice of receipt", "received", "filed",
        "submitted", "application", "draft", "proposed", "hearing",
        "contested case", "response to comments", "awaiting", "incomplete",
        "not started",
        # ERCOT queue milestones. These read as progress, not authorization:
        # a project with its screening study complete or its security posted
        # is still waiting on an interconnection agreement.
        "screening", "screening complete", "ss complete", "study in progress",
        "under study", "requested", "in queue", "fis approved", "fis requested",
        "security posted", "financial security",
    ]),
    (OPERATING, [
        "energized", "in service", "in-service", "commercial operation",
        "operational", "operating", "commenced operation", "online",
    ]),
    (APPROVED, [
        "approved", "issued", "granted", "final", "effective", "registered",
        "registration complete", "authorized", "authorization granted",
        "permitted", "complete", "signed", "executed", "active",
    ]),
]

# Finer stage within a lifecycle. Ordered most specific first.
_STAGE_PHRASES = [
    ("contested_case_hearing", ["contested case", "hearing requested", "hearing"]),
    ("response_to_comments", ["response to comments", "rtc"]),
    ("public_notice", ["public notice", "public comment", "comment period",
                       "notice of application", "notice of receipt"]),
    ("technical_review", ["technical review", "under technical review"]),
    ("administrative_review", ["administrative review", "completeness review",
                               "admin review"]),
    ("draft_permit", ["draft permit", "draft"]),
    ("executive_director_decision", ["executive director", "ed decision",
                                     "final review"]),
    # ERCOT interconnection milestones.
    ("interconnection_agreement", ["ia signed", "interconnection agreement",
                                   "agreement signed"]),
    ("financial_security", ["financial security", "security posted"]),
    ("full_interconnection_study", ["fis approved", "fis requested",
                                    "full interconnection study", "fis"]),
    ("screening_study", ["screening study", "ss complete", "screening"]),
    ("energization", ["approved for energization", "energization"]),
    ("received", ["received", "filed", "submitted"]),
]


def classify_lifecycle(status=None, program=None, decision_date=None,
                       received_date=None, source=None):
    """Return (lifecycle, stage).

    `decision_date` acts as a tiebreaker: a record with a final-action date but
    unreadable status text is treated as approved, since agencies do not date a
    decision that has not happened. A received date alone implies pending.
    """
    haystack = norm_text(status)

    lifecycle = None
    for candidate, phrases in _LIFECYCLE_PHRASES:
        if _matches(haystack, phrases):
            lifecycle = candidate
            break

    if lifecycle is None:
        if decision_date:
            lifecycle = APPROVED
        elif received_date:
            lifecycle = PENDING
        else:
            lifecycle = UNKNOWN

    stage = None
    for candidate, phrases in _STAGE_PHRASES:
        if _matches(haystack, phrases):
            stage = candidate
            break

    return lifecycle, stage


def _matches(haystack_norm, phrases):
    """Whole-phrase containment on already-normalized text."""
    if not haystack_norm:
        return False
    padded = f" {haystack_norm} "
    for phrase in phrases:
        phrase_norm = norm_text(phrase)
        if phrase_norm and f" {phrase_norm} " in padded:
            return True
    return False


# --- Authorization program ---------------------------------------------------

PROGRAM_NSR = "nsr"                     # 30 TAC 116 case-by-case air permit
PROGRAM_PSD = "psd"                     # major source prevention of significant deterioration
PROGRAM_NNSR = "nonattainment_nsr"      # major source in a nonattainment area
PROGRAM_PBR = "pbr"                     # 30 TAC 106 permit by rule
PROGRAM_STANDARD = "standard_permit"    # 30 TAC 116 Subchapter F
PROGRAM_TITLE_V = "title_v"             # 30 TAC 122 federal operating permit
PROGRAM_DE_MINIMIS = "de_minimis"
PROGRAM_OTHER_AIR = "other_air"
PROGRAM_STORMWATER = "stormwater_cgp"   # TXR150000 construction general permit
PROGRAM_INTERCONNECTION = "interconnection"

AIR_PROGRAMS = [
    PROGRAM_NSR, PROGRAM_PSD, PROGRAM_NNSR, PROGRAM_PBR, PROGRAM_STANDARD,
    PROGRAM_TITLE_V, PROGRAM_DE_MINIMIS, PROGRAM_OTHER_AIR,
]

# PSD and nonattainment NSR are checked before plain NSR: they are major-source
# flavours of it and the specific label is the useful one.
_PROGRAM_PHRASES = [
    (PROGRAM_PSD, ["psd", "prevention of significant deterioration"]),
    (PROGRAM_NNSR, ["nonattainment", "non attainment", "nnsr",
                    "emission offset", "eor"]),
    (PROGRAM_TITLE_V, ["title v", "title 5", "federal operating permit",
                       "site operating permit", "general operating permit",
                       "fop", "sop", "gop", "chapter 122"]),
    (PROGRAM_PBR, ["permit by rule", "permits by rule", "pbr", "chapter 106",
                   "30 tac 106"]),
    (PROGRAM_STANDARD, ["standard permit", "air quality standard permit"]),
    (PROGRAM_DE_MINIMIS, ["de minimis", "de-minimis"]),
    (PROGRAM_NSR, ["new source review", "nsr", "air quality permit",
                   "case by case", "chapter 116", "30 tac 116"]),
    (PROGRAM_STORMWATER, ["txr150000", "txr15", "stormwater", "storm water",
                          "construction general permit"]),
    (PROGRAM_INTERCONNECTION, ["interconnection", "interconnect"]),
]


def classify_program(permit_type=None, permit_number=None, source=None):
    """Return the canonical authorization program, or None.

    Permit numbers carry the program too: TCEQ registrations are prefixed 'PBR',
    stormwater NOIs 'TXR15', so a permit type left blank is still classifiable.
    """
    haystack = " ".join(norm_text(part) for part in (permit_type, permit_number) if part)

    for candidate, phrases in _PROGRAM_PHRASES:
        if _matches(haystack, phrases):
            return candidate

    # Fall back to the permit number's prefix, which is not whitespace-delimited
    # and so is invisible to whole-phrase matching.
    number = norm_text(permit_number).replace(" ", "")
    if number.startswith("txr15"):
        return PROGRAM_STORMWATER
    if number.startswith("pbr"):
        return PROGRAM_PBR
    if number.startswith(("inr", "lir")) or (number[:2].isdigit() and "inr" in number):
        return PROGRAM_INTERCONNECTION

    # Source-level defaults, so every record lands in a program.
    return {
        "tceq_air": PROGRAM_OTHER_AIR,
        "tceq_swnoi": PROGRAM_STORMWATER,
        "ercot_gis": PROGRAM_INTERCONNECTION,
        "ercot_large_load": PROGRAM_INTERCONNECTION,
    }.get(source)


# --- Permit action -----------------------------------------------------------

ACTION_NEW = "new"
ACTION_AMENDMENT = "amendment"
ACTION_ALTERATION = "alteration"
ACTION_RENEWAL = "renewal"
ACTION_REVISION = "revision"
ACTION_REGISTRATION = "registration"
ACTION_CHANGE_OF_LOCATION = "change_of_location"
ACTION_REVOCATION = "revocation"

_ACTION_PHRASES = [
    (ACTION_CHANGE_OF_LOCATION, ["change of location", "relocation"]),
    (ACTION_AMENDMENT, ["amendment", "amend", "amended"]),
    (ACTION_ALTERATION, ["alteration", "alter", "altered", "modification",
                         "modify", "modified"]),
    (ACTION_RENEWAL, ["renewal", "renew", "renewed"]),
    (ACTION_REVISION, ["revision", "revise", "revised", "significant revision",
                       "minor revision"]),
    (ACTION_REVOCATION, ["revocation", "revoke"]),
    (ACTION_REGISTRATION, ["registration", "register", "notification"]),
    (ACTION_NEW, ["new source review", "new permit", "initial", "new"]),
]


def classify_action(permit_type=None, description=None):
    """Return the canonical permit action, or None when nothing says."""
    haystack = " ".join(norm_text(part) for part in (permit_type, description) if part)
    for candidate, phrases in _ACTION_PHRASES:
        if _matches(haystack, phrases):
            return candidate
    return None


# --- Display -----------------------------------------------------------------

def days_sitting(received_date, as_of=None):
    """How long an application has been in process, in days. None if unknown.

    Deliberately computed on read rather than stored — the answer changes every
    day, and a stored value would go stale the moment the database is written.
    """
    if not received_date:
        return None
    try:
        filed = datetime.strptime(str(received_date)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    reference = as_of or date.today()
    if isinstance(reference, datetime):
        reference = reference.date()
    return (reference - filed).days


def summarize_lifecycle(lifecycle):
    return {
        PENDING: "Application pending",
        APPROVED: "Approved / issued",
        OPERATING: "Operating",
        WITHDRAWN: "Withdrawn / void",
        DENIED: "Denied",
        EXPIRED: "Expired / terminated",
        UNKNOWN: "Status unknown",
    }.get(lifecycle, "Status unknown")


def summarize_program(program):
    return {
        PROGRAM_NSR: "Air NSR permit",
        PROGRAM_PSD: "Air NSR — PSD (major)",
        PROGRAM_NNSR: "Air NSR — nonattainment (major)",
        PROGRAM_PBR: "Permit by rule",
        PROGRAM_STANDARD: "Standard permit",
        PROGRAM_TITLE_V: "Title V operating permit",
        PROGRAM_DE_MINIMIS: "De minimis",
        PROGRAM_OTHER_AIR: "Other air authorization",
        PROGRAM_STORMWATER: "Stormwater construction NOI",
        PROGRAM_INTERCONNECTION: "ERCOT interconnection",
    }.get(program, "Unclassified")


def _pretty(code):
    """'technical_review' -> 'Technical review'. None for anything not a string.

    The isinstance guard matters: a null in a pandas column arrives here as a
    float NaN, which is falsy-adjacent but still has no .replace.
    """
    if not isinstance(code, str) or not code.strip():
        return None
    return code.replace("_", " ").capitalize()


def summarize_stage(stage):
    return _pretty(stage)


def summarize_action(action):
    return _pretty(action)
