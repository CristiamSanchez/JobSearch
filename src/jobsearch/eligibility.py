"""Rule-based eligibility analysis (Phase 3a).

Implements the "Candidate eligibility rules" section of the README for a
single posting:

* the documented location rules, inspecting both the structured
  ``JobPosting.location`` field and the complete ``JobPosting.description``;
* detection of explicit work-authorization, citizenship, residency, and
  sponsorship requirements, classified into the five
  :class:`AuthorizationSignal` values instead of a blanket rejection.

Pure standard-library regex — no AI, no APIs, no external dependencies.
The outcome is a categorical :class:`EligibilityStatus` backed by concrete
evidence in ``reasons``/``signals``; eligibility is never expressed as a
score (professional match is the separate, score-bearing concept).
"""

from __future__ import annotations

import re

from .models import (
    AuthorizationSignal,
    CandidateProfile,
    EligibilityResult,
    EligibilityStatus,
    JobPosting,
)

__all__ = ["evaluate_eligibility"]

# --- Internal vocabularies ----------------------------------------------------

_US = "US"
_LATAM = "LATAM"
_AMERICAS = "AMERICAS"
_WORLDWIDE = "WORLDWIDE"

_REMOTE = "remote"
_HYBRID = "hybrid"
_ONSITE = "onsite"
_MODE_CONFLICT = "mode_conflict"

# Combining the location and authorization verdicts: most severe wins.
_SEVERITY = {
    EligibilityStatus.ELIGIBLE: 0,
    EligibilityStatus.UNKNOWN: 1,
    EligibilityStatus.REVIEW_REQUIRED: 2,
    EligibilityStatus.NOT_ELIGIBLE: 3,
}

# What each requirement kind needs from the candidate profile.
_KIND_FACTS = {
    "citizenship": ("US citizenship", "the candidate is not a US citizen"),
    "residency": (
        "US permanent residency (green card)",
        "the candidate has no green card or permanent residency",
    ),
    "work_auth": (
        "US work authorization",
        "the candidate has no current US work authorization",
    ),
}

# Matches "us", "usa", "u.s.", "u.s.a." and "united states" inside phrases.
_US_TOKEN = r"(?:u\.?\s?s\.?(?:\s*a)?\.?|united\s+states)"

# --- Work-mode detection ------------------------------------------------------

_HYBRID_RE = re.compile(r"\bhybrid\b")
_ONSITE_RE = re.compile(
    r"\bon[\s-]?site\b|\bin[\s-]?person\b|\bwork[\s-]?from[\s-]?office\b|\btelecommut"
)
_REMOTE_RE = re.compile(
    r"\bremote(?:ly)?\b|\bwork[\s-]?from[\s-]?home\b|\bwfh\b|\btelecommut"
)

# --- Region detection ---------------------------------------------------------

_TIMEZONE_RE = re.compile(r"time[\s-]?zones?|timezone")

# Regions read straight from the structured location field ("Remote - LATAM").
_LOCATION_REGION_RES = (
    (_US, re.compile(r"\b(?:us|usa)\b|u\.s\.|united\s+states")),
    (_LATAM, re.compile(r"\blatam\b|latin[\s-]?america|central[\s-]?america")),
    (_AMERICAS, re.compile(r"\b(?:north|south)[\s-]?america|\bamericas?\b")),
    (
        _WORLDWIDE,
        re.compile(r"\bworldwide\b|\bglobally\b|\bglobal\b|\banywhere\b|\bany\s+location\b"),
    ),
)

# Description units must look like a location statement before a region counts.
_GEO_CUE_RE = re.compile(
    r"remote|candidate|applicant|must|should|requir\w+|eligible|resid\w+|living|"
    r"role|position|hiring|employees?|team|join|open\s+to|relocat\w+"
)

# Regions read from the description (unit must contain a geo cue).
_DESC_REGION_RES = (
    (_US, re.compile(r"(?:in|within|across|throughout|inside)\s+the\s+" + _US_TOKEN + r"\b")),
    (_US, re.compile(r"\b" + _US_TOKEN + r"[\s-]+only\b")),
    (_US, re.compile(r"\b" + _US_TOKEN + r"\s+(?:residents?|nationals?)\s+only\b")),
    (
        _US,
        re.compile(
            r"\b(?:based|located|resid\w+|liv\w+|relocate|relocation|moving)"
            r"\s+(?:in|within|to)\s+(?:the\s+)?" + _US_TOKEN + r"\b"
        ),
    ),
    (_LATAM, re.compile(r"\blatam\b|latin[\s-]?america|central[\s-]?america")),
    (_AMERICAS, re.compile(r"\b(?:north|south)[\s-]?america|\bamericas?\b")),
    (
        _WORLDWIDE,
        re.compile(r"\bworldwide\b|\bglobally\b|\bglobal\b|\banywhere\b|\bany\s+location\b"),
    ),
)

# --- Authorization / citizenship / residency requirements ---------------------

# Each entry: (requirement kind, pattern). Only jurisdiction-explicit or
# US-specific phrasings count as hard requirements; bare mentions do not.
_AUTH_REQUIREMENT_RES = (
    # US citizenship
    ("citizenship", re.compile(r"\b(?:must\s+be|be|are|only)\s+(?:a\s+)?" + _US_TOKEN + r"\s+citizens?\b")),
    ("citizenship", re.compile(r"\b" + _US_TOKEN + r"\s+citizenship\s+(?:is\s+)?(?:required|necessary|mandatory)\b")),
    ("citizenship", re.compile(r"\b" + _US_TOKEN + r"\s+citizens?\s+only\b")),
    # Green card / permanent residency
    (
        "residency",
        re.compile(
            r"\b(?:must|should|required\s+to|need\s+to|have\s+to)\s+(?:have|hold|be)"
            r"\s+(?:a\s+|valid\s+|current\s+)?(?:lawful\s+)?(?:green\s+card|permanent\s+resident)\b"
        ),
    ),
    (
        "residency",
        re.compile(
            r"\b(?:green\s+card|permanent\s+resident)(?:\s+status)?\s+(?:is\s+)?"
            r"(?:required|necessary|mandatory|must\s+have)\b"
        ),
    ),
    ("residency", re.compile(r"\b(?:lawful\s+permanent\s+resident|green\s+card\s+holder)s?\s+(?:only|status\s+required)\b")),
    ("residency", re.compile(r"\bmust\s+be\s+(?:a\s+)?" + _US_TOKEN + r"\s+(?:lawful\s+)?permanent\s+resident\b")),
    # US work authorization (jurisdiction explicit)
    ("work_auth", re.compile(r"\bauthori[sz]ed\s+to\s+work\s+in\s+the\s+" + _US_TOKEN + r"\b")),
    (
        "work_auth",
        re.compile(
            r"\b(?:must|should|required\s+to|need\s+to)\s+(?:already\s+)?(?:have|hold|possess)"
            r"(?:\s+(?:valid|current|existing))?\s+(?:u\.?\s?s\.?(?:\s*a)?\.?\s+)?"
            r"(?:work|employment)\s+authori[sz]ation\b"
        ),
    ),
    ("work_auth", re.compile(r"\b" + _US_TOKEN + r"\s+(?:work|employment)\s+authori[sz]ation\s+(?:is\s+)?(?:required|necessary|mandatory)\b")),
    ("work_auth", re.compile(r"\brequires?\s+(?:a\s+|valid\s+|current\s+)?" + _US_TOKEN + r"\s+(?:work|employment)\s+authori[sz]ation\b")),
    ("work_auth", re.compile(r"\b(?:must|should)\s+be\s+(?:eligible|able)\s+to\s+work\s+in\s+the\s+" + _US_TOKEN + r"\b")),
    # Existing US visa/instrument status (inherently US-specific)
    (
        "work_auth",
        re.compile(
            r"\b(?:must|should|required\s+to|currently)\s+(?:have|hold|be\s+in\s+possession\s+of)"
            r"\s+(?:a\s+|an\s+|the\s+)?(?:valid\s+|current\s+|existing\s+)*"
            r"(?:h-?1b|h1b|ead|opt|cpt|visa\s+status)\b"
        ),
    ),
    ("work_auth", re.compile(r"\b(?:h-?1b|h1b|ead|opt|cpt)\s+(?:status\s+|visa\s+)?(?:is\s+)?(?:required|necessary|mandatory|needed)\b")),
)

# Explicit statements that no authorization/citizenship is needed.
_SATISFIED_RES = (
    re.compile(
        r"\bno\s+(?:prior\s+|previous\s+|existing\s+|initial\s+)?"
        r"(?:u\.?\s?s\.?(?:\s*a)?\.?\s+)?(?:work|employment)\s+authori[sz]ation"
        r"\s+(?:is\s+)?(?:required|necessary|needed)\b"
    ),
    re.compile(r"\bauthori[sz]ation\s+is\s+not\s+(?:required|necessary|needed)\b"),
    re.compile(
        r"\b(?:does|do|did)\s+not\s+(?:need|require)\w*\s+[^.;\n]{0,60}?"
        r"\b(?:authori[sz]ation|citizenship|green\s+card)\b"
    ),
    re.compile(r"\bno\s+(?:prior\s+|previous\s+)?(?:u\.?\s?s\.?(?:\s*a)?\.?\s+)?citizenship\s+(?:is\s+)?(?:required|necessary|needed)\b"),
    re.compile(r"\bno\s+(?:prior\s+|previous\s+)?(?:u\.?\s?s\.?(?:\s*a)?\.?\s+)?(?:green\s+card|visa)\s+(?:is\s+)?(?:required|needed)\b"),
)

# Sponsorship stance (checked negative-first per text unit).
_SPONSORSHIP_UNAVAILABLE_RES = (
    re.compile(r"\b(?:no|not|without|never)\s+(?:any\s+|visa\s+|h-?1b\s+|such\s+)?sponsorship\b"),
    re.compile(r"\b(?:does|do|can|will|would|is|are)\s+(?:also\s+)?not\s+(?:sponsor|provide|offer|support|fund)\b"),
    re.compile(
        r"\b(?:cannot|can't|won't|unable\s+to|not\s+able\s+to|not\s+willing(?:\s+to)?)"
        r"\s+(?:to\s+)?(?:sponsor|provide\s+(?:visa\s+)?sponsorship|offer\s+sponsorship)\b"
    ),
    re.compile(r"\b(?:not|never)\s+sponsoring\b"),
    re.compile(
        r"\bsponsorship\s+(?:is\s+)?(?:not\s+available|unavailable|not\s+offered|"
        r"not\s+provided|not\s+possible|not\s+an\s+option)\b"
    ),
)

_SPONSORSHIP_AVAILABLE_RES = (
    re.compile(
        r"(?<!no\s)(?<!not\s)\b(?:visa\s+|h-?1b\s+)?sponsorship\s+(?:is\s+)?"
        r"(?:available|offered|provided|possible|considered|case[\s-]by[\s-]case)\b"
    ),
    re.compile(
        r"\b(?:we|the\s+(?:company|employer)|our\s+(?:team|company))"
        r"\s+(?:typically\s+|usually\s+|gladly\s+|happily\s+|also\s+|may\s+)?"
        r"(?:will\s+|can\s+|do\s+|does\s+|are\s+)?sponsor\b"
    ),
    re.compile(
        r"\b(?:offer|offers|offering|provide|provides|providing|arrange|arranges|fund|funds)"
        r"\s+(?:visa\s+|h-?1b\s+)?sponsorship\b"
    ),
    re.compile(r"\bsponsor\s+(?:h-?1b|visas?|work\s+visas?|employment\s+visas?|selected|qualified)\b"),
)

# Bare authorization terms (used only to detect ambiguous wording).
_MENTION_RE = re.compile(
    r"\bvisa\b|\bsponsorship\b|\bsponsoring\b|\bsponsor(?:s|ed)?\b|"
    r"\bauthori[sz]ed\b|\bauthori[sz]ation\b|\bead\b|\bopt\b|\bcpt\b|\bh-?1b\b|"
    r"\bgreen\s+card\b|\bcitizenship\b|\bcitizen\b|\bpermanent\s+resident\b"
)

# Words that turn a mention into a requirement claim / or soften one.
_HARD_RE = re.compile(
    r"\b(?:must|requir\w+|need\w+|only|eligib\w+|qualif\w+|mandatory|necessary|essential)\b"
)
_SOFTENING_RE = re.compile(
    r"\b(?:welcome|encouraged|encouragement|regardless|invited\s+to\s+apply|"
    r"open\s+to\s+applications|we\s+support)\b"
)


# --- Helpers ------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase and rewrite wording that would confuse phrase matching.

    Abbreviations such as "U.S." are unfolded so sentence splitting on
    periods does not break them apart.
    """
    return (
        text.lower()
        .replace("united states of america", "united states")
        .replace("u.s.a.", "usa")
        .replace("u.s.a", "usa")
        .replace("u.s.", "us")
        .replace("u.s", "us")
        .replace(":", " ")
    )


def _split_units(text: str) -> list[str]:
    """Split text into sentences/lines suitable for phrase inspection."""
    return [unit.strip() for unit in re.split(r"[.!?\n;]+", _normalize(text)) if unit.strip()]


def _first_match(
    patterns: tuple[re.Pattern[str], ...], text: str
) -> re.Match[str] | None:
    for pattern in patterns:
        if match := pattern.search(text):
            return match
    return None


def _first_requirement(text: str) -> tuple[str, str] | None:
    """Return (requirement kind, matched text) for a hard requirement."""
    for kind, pattern in _AUTH_REQUIREMENT_RES:
        if match := pattern.search(text):
            return kind, match.group(0)
    return None


def _detect_mode(text: str) -> str | None:
    """Classify the work mode of a text blob, if it states one."""
    if _HYBRID_RE.search(text):
        return _HYBRID
    remote = bool(_REMOTE_RE.search(text))
    onsite = bool(_ONSITE_RE.search(text))
    if remote and onsite:
        return _MODE_CONFLICT
    if remote:
        return _REMOTE
    if onsite:
        return _ONSITE
    return None


def _find_regions(location_text: str, description: str) -> list[tuple[str, str]]:
    """Collect (region kind, matched text) evidence from both fields."""
    location_text = _normalize(location_text)
    evidence: list[tuple[str, str]] = []

    # Structured location field: bare region terms define the declared scope.
    location_tz = bool(_TIMEZONE_RE.search(location_text))
    for kind, pattern in _LOCATION_REGION_RES:
        if kind == _US and location_tz:
            continue  # e.g. "Remote - US time zone" is not a US-only restriction
        if match := pattern.search(location_text):
            evidence.append((kind, match.group(0)))

    # Description: a region only counts inside a location-shaped statement.
    for unit in _split_units(description):
        if not _GEO_CUE_RE.search(unit):
            continue
        unit_tz = bool(_TIMEZONE_RE.search(unit))
        for kind, pattern in _DESC_REGION_RES:
            if kind == _US and unit_tz:
                continue
            if match := pattern.search(unit):
                evidence.append((kind, match.group(0)))

    return evidence


def _candidate_holds(kind: str, profile: CandidateProfile) -> bool:
    if kind == "citizenship":
        return profile.us_citizenship
    if kind == "residency":
        return profile.us_green_card
    return (
        profile.us_work_authorization
        or profile.us_green_card
        or profile.us_citizenship
        or profile.us_opt
    )


def _candidate_in_latam(profile: CandidateProfile) -> bool:
    region = profile.region.strip().upper()
    return "LATAM" in region or "LATIN" in region


# --- Evaluators ---------------------------------------------------------------


def _evaluate_location(
    posting: JobPosting, profile: CandidateProfile
) -> tuple[EligibilityStatus, list[str], list[AuthorizationSignal]]:
    """Apply the documented location rules to the posting."""
    reasons: list[str] = []
    signals: list[AuthorizationSignal] = []
    location = posting.location

    mode = _detect_mode(_normalize(location)) or _detect_mode(posting.description)
    if mode is None:
        reasons.append(
            f"Work mode could not be determined from location {location!r} or the description"
        )
        return EligibilityStatus.UNKNOWN, reasons, signals
    if mode == _HYBRID:
        reasons.append(f"Work mode is hybrid (location {location!r}); in-person presence required")
        return EligibilityStatus.NOT_ELIGIBLE, reasons, signals
    if mode == _ONSITE:
        reasons.append(
            f"Work mode is on-site/in-person (location {location!r}); in-person presence required"
        )
        return EligibilityStatus.NOT_ELIGIBLE, reasons, signals
    if mode == _MODE_CONFLICT:
        reasons.append(
            f"Conflicting work modes in location {location!r} (both remote and on-site present)"
        )
        return EligibilityStatus.REVIEW_REQUIRED, reasons, signals

    evidence = _find_regions(location, posting.description)
    kinds = {kind for kind, _ in evidence}
    us_evidence = [matched for kind, matched in evidence if kind == _US]
    regional_evidence = [matched for kind, matched in evidence if kind in (_LATAM, _AMERICAS)]

    if _US in kinds and regional_evidence:
        signals.append(AuthorizationSignal.AUTHORIZATION_REQUIRED)
        reasons.append(
            f"Conflicting geographic restrictions: United States ({us_evidence[0]!r}) vs. "
            f"regional scope ({regional_evidence[0]!r}) in location {location!r} and the description"
        )
        return EligibilityStatus.REVIEW_REQUIRED, reasons, signals
    if _US in kinds:
        signals.append(AuthorizationSignal.AUTHORIZATION_REQUIRED)
        reasons.append(
            f"Role is restricted to the United States (matched {us_evidence[0]!r}) "
            f"in location {location!r}"
        )
        reasons.append(
            f"Restriction clearly applies: the candidate is based in {profile.country} "
            "with no US work authorization or US residency"
        )
        return EligibilityStatus.NOT_ELIGIBLE, reasons, signals

    latam_evidence = [matched for kind, matched in evidence if kind == _LATAM]
    if latam_evidence:
        if _candidate_in_latam(profile):
            reasons.append(
                f"Location {location!r} is remote within the candidate's region "
                f"(matched {latam_evidence[0]!r})"
            )
            return EligibilityStatus.ELIGIBLE, reasons, signals
        reasons.append(
            f"Role is limited to LATAM (matched {latam_evidence[0]!r}) but the candidate "
            f"region is {profile.region}"
        )
        return EligibilityStatus.REVIEW_REQUIRED, reasons, signals

    americas_evidence = [matched for kind, matched in evidence if kind == _AMERICAS]
    if americas_evidence:
        reasons.append(
            f"Role covers the Americas (matched {americas_evidence[0]!r}) — potentially "
            "eligible; review if restrictions exist"
        )
        return EligibilityStatus.REVIEW_REQUIRED, reasons, signals

    worldwide_evidence = [matched for kind, matched in evidence if kind == _WORLDWIDE]
    if worldwide_evidence:
        reasons.append(
            f"Role is open worldwide (matched {worldwide_evidence[0]!r}) — potentially "
            "eligible; review for restrictions"
        )
        return EligibilityStatus.REVIEW_REQUIRED, reasons, signals

    reasons.append(
        f"Remote scope is not specified in location {location!r} or the description; "
        "verify geographic restrictions"
    )
    return EligibilityStatus.REVIEW_REQUIRED, reasons, signals


def _evaluate_authorization(
    posting: JobPosting, profile: CandidateProfile
) -> tuple[EligibilityStatus | None, list[str], list[AuthorizationSignal]]:
    """Detect and interpret explicit authorization requirements."""
    reasons: list[str] = []
    signals: list[AuthorizationSignal] = []

    requirements: list[tuple[str, str]] = []
    satisfied: list[str] = []
    available: list[str] = []
    unavailable: list[str] = []
    ambiguous: list[str] = []

    units = _split_units(posting.location) + _split_units(posting.description)
    for unit in units:
        # Explicit "not required" statements dominate their unit.
        satisfied_match = _first_match(_SATISFIED_RES, unit)
        if satisfied_match:
            satisfied.append(satisfied_match.group(0))
            continue

        requirement = _first_requirement(unit)
        if requirement:
            kind, matched = requirement
            if _SOFTENING_RE.search(unit) and not _HARD_RE.search(unit):
                ambiguous.append(matched)
            else:
                requirements.append((kind, matched))

        # Sponsorship stance: negative first, so "do not sponsor" is not read
        # as an offer. A mere mention without stance is handled below.
        unavailable_match = _first_match(_SPONSORSHIP_UNAVAILABLE_RES, unit)
        if unavailable_match:
            unavailable.append(unavailable_match.group(0))
            continue
        available_match = _first_match(_SPONSORSHIP_AVAILABLE_RES, unit)
        if available_match:
            available.append(available_match.group(0))
            continue

        if requirement:
            continue
        mention = _MENTION_RE.search(unit)
        if mention and _HARD_RE.search(unit):
            ambiguous.append(unit)

    unmet = [(kind, text) for kind, text in requirements if not _candidate_holds(kind, profile)]
    met = [(kind, text) for kind, text in requirements if _candidate_holds(kind, profile)]

    if unmet:
        kind, matched = unmet[0]
        need, detail = _KIND_FACTS[kind]
        if available:
            # A requirement contradicted by an offer of sponsorship is not a
            # clear rejection — route it to human review instead.
            status = EligibilityStatus.REVIEW_REQUIRED
            signals.extend(
                (
                    AuthorizationSignal.AUTHORIZATION_REQUIRED,
                    AuthorizationSignal.SPONSORSHIP_AVAILABLE,
                )
            )
            reasons.append(
                f"Requirement matched ({matched!r}) requires {need}, but sponsorship is "
                f"also offered ({available[0]!r}) — review the authorization path"
            )
        else:
            status = EligibilityStatus.NOT_ELIGIBLE
            signals.append(AuthorizationSignal.AUTHORIZATION_REQUIRED)
            reasons.append(
                f"Explicit requirement not satisfied: matched {matched!r} "
                f"({need} required; {detail})"
            )
            if unavailable:
                signals.append(AuthorizationSignal.SPONSORSHIP_UNAVAILABLE)
                reasons.append(
                    f"Employer does not offer sponsorship: matched {unavailable[0]!r}"
                )
    elif met or satisfied:
        status = EligibilityStatus.ELIGIBLE
        signals.append(AuthorizationSignal.REQUIREMENT_SATISFIED)
        evidence = met[0][1] if met else satisfied[0]
        reasons.append(f"Requirement explicitly satisfied: matched {evidence!r}")
    elif unavailable or available or ambiguous:
        status = EligibilityStatus.REVIEW_REQUIRED
        if unavailable:
            signals.append(AuthorizationSignal.SPONSORSHIP_UNAVAILABLE)
            reasons.append(
                f"Sponsorship is not available: matched {unavailable[0]!r} — confirm "
                "whether the role requires US work authorization"
            )
        if available:
            signals.append(AuthorizationSignal.SPONSORSHIP_AVAILABLE)
            reasons.append(
                f"Employer offers sponsorship: matched {available[0]!r} — confirm the "
                "candidate can use that path"
            )
        if ambiguous:
            signals.append(AuthorizationSignal.AMBIGUOUS)
            reasons.append(f"Ambiguous authorization wording: matched {ambiguous[0]!r}")
    else:
        status = None

    return status, reasons, signals


# --- Public API ---------------------------------------------------------------


def evaluate_eligibility(posting: JobPosting, profile: CandidateProfile) -> EligibilityResult:
    """Evaluate whether ``profile`` can realistically apply to ``posting``.

    Rule-based only: the structured ``location`` field *and* the complete
    description are inspected for location scope and explicit
    work-authorization, citizenship, residency, and sponsorship requirements.

    Returns a categorical :class:`EligibilityStatus` with concrete evidence in
    ``reasons`` and ``signals``. The result never contains a score —
    professional match is a separate concept (:class:`ProfessionalMatch`).
    Ambiguous evidence yields ``REVIEW_REQUIRED`` or ``UNKNOWN`` rather than an
    unsupported decision.
    """
    location_status, location_reasons, location_signals = _evaluate_location(posting, profile)
    auth_status, auth_reasons, auth_signals = _evaluate_authorization(posting, profile)

    statuses = [location_status]
    if auth_status is not None:
        statuses.append(auth_status)
    status = max(statuses, key=lambda value: _SEVERITY[value])

    reasons = location_reasons + auth_reasons
    if auth_status is None:
        reasons.append(
            "No explicit work-authorization, citizenship, residency, or "
            "sponsorship requirement found"
        )

    # Deduplicate while preserving order.
    signals = list(dict.fromkeys([*location_signals, *auth_signals]))
    if (
        status is EligibilityStatus.ELIGIBLE
        and AuthorizationSignal.REQUIREMENT_SATISFIED not in signals
    ):
        signals.append(AuthorizationSignal.REQUIREMENT_SATISFIED)
        reasons.append(
            f"Location {posting.location!r} matches the candidate's region "
            f"({profile.region}) and no restrictive requirement was found"
        )

    return EligibilityResult(
        status=status, reasons=tuple(reasons), signals=tuple(signals)
    )
