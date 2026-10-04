"""Deterministic professional matching engine (Phase 3b).

Compares the structured requirements of a :class:`JobPosting` against a
:class:`CandidateProfile` and produces a :class:`ProfessionalMatch` whose
score always lies in ``[0.0, 1.0]``.

The formula is fully explicit — no AI, no free-text guessing::

    score = SUM(weight_d * subscore_d) / SUM(weight_d)   over assessed dimensions

* A dimension the posting does not state is **not assessed**: its weight is
  removed and the remaining weights are renormalized.
* A dimension the posting states but the profile cannot demonstrate scores
  **0.0** (missing data never earns credit).
* Required vs preferred skills are separate dimensions with separate weights;
  only required misses become interview topics.

Free-text requirement extraction from ``description`` is deliberately out of
scope here — requirements come from the posting's structured fields.

Eligibility is never part of this score; that is
:func:`jobsearch.eligibility.evaluate_eligibility`.
"""

from __future__ import annotations

import re

from .models import CandidateProfile, JobPosting, ProfessionalMatch

__all__ = ["DIMENSION_WEIGHTS", "evaluate_professional_match"]

# Documented weights; they sum to 1.0 (asserted by the test suite).
DIMENSION_WEIGHTS: dict[str, float] = {
    "required_skills": 0.35,
    "preferred_skills": 0.10,
    "experience": 0.20,
    "technologies": 0.15,
    "education": 0.08,
    "certifications": 0.05,
    "languages": 0.07,
}

_DIMENSION_ORDER = tuple(DIMENSION_WEIGHTS)

# Deterministic synonym normalization, applied after basic normalization.
_SKILL_ALIASES = {
    "k8s": "kubernetes",
    "js": "javascript",
    "ts": "typescript",
    "postgres": "postgresql",
    "golang": "go",
    "reactjs": "react",
    "vuejs": "vue",
    "nodejs": "node js",
    "nextjs": "next js",
    "expressjs": "express",
}

# Education hierarchy: a higher degree satisfies a lower requirement.
# bachelor (2) meets associate (1); master (3) meets bachelor (2); and an
# adjacent-but-lower degree counts as a partial match (0.5).
_EDUCATION_RANKS = (
    (4, re.compile(r"\bph\.?d\.?\b|doctorate|doctoral")),
    (3, re.compile(r"\bmaster\b|\bmba\b|maestr")),
    (2, re.compile(r"\bbachelor\b|licenciatura|undergraduate")),
    (1, re.compile(r"\bassociate\b|tecn[o[o]log")),
)


def _normalize_item(text: str) -> str:
    """Lowercase, replace separators with spaces, apply known synonyms."""
    normalized = re.sub(r"[-_./]+", " ", text.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return _SKILL_ALIASES.get(normalized, normalized)


def _shares_signal(left: str, right: str) -> bool:
    """True when two normalized items share a meaningful token or contain
    one another (partial match = 0.5)."""
    left_tokens = {token for token in left.split() if len(token) >= 3}
    right_tokens = {token for token in right.split() if len(token) >= 3}
    if left_tokens and right_tokens and (left_tokens & right_tokens):
        return True
    if len(left) >= 4 and len(right) >= 4 and (left in right or right in left):
        return True
    return False


def _score_items(
    requirements: tuple[str, ...], candidates: tuple[str, ...]
) -> tuple[float, list[str], list[str], list[str]]:
    """Score job items against a candidate pool.

    Returns (subscore, exact matches, partial matches, missing items).
    Exact match = 1.0, partial match = 0.5, no match = 0.0.
    """
    normalized = [_normalize_item(candidate) for candidate in candidates]
    matched: list[str] = []
    partial: list[str] = []
    missing: list[str] = []
    total = 0.0

    for item in requirements:
        target = _normalize_item(item)
        if target in normalized:
            total += 1.0
            matched.append(item)
        elif any(_shares_signal(target, candidate) for candidate in normalized):
            total += 0.5
            partial.append(item)
        else:
            missing.append(item)

    score = total / len(requirements) if requirements else 0.0
    return score, matched, partial, missing


def _education_rank(text: str) -> int | None:
    lowered = text.lower()
    for rank, pattern in _EDUCATION_RANKS:
        if pattern.search(lowered):
            return rank
    return None


def _education_item_score(required: str, profile_education: tuple[str, ...]) -> float:
    if not profile_education:
        return 0.0

    required_rank = _education_rank(required)
    profile_ranks = [
        rank
        for rank in (_education_rank(item) for item in profile_education)
        if rank is not None
    ]
    if required_rank is not None and profile_ranks:
        best = max(profile_ranks)
        if best >= required_rank:
            return 1.0
        if best == required_rank - 1:
            return 0.5
        return 0.0

    # Neither side maps to a known level: fall back to item matching.
    score, _, _, _ = _score_items((required,), profile_education)
    return score


def _dedupe(items) -> tuple[str, ...]:
    """Deduplicate while preserving order (deterministic output)."""
    return tuple(dict.fromkeys(items))


def _fmt_years(value: float) -> str:
    return f"{value:g}"


def evaluate_professional_match(
    posting: JobPosting, profile: CandidateProfile
) -> ProfessionalMatch:
    """Score how well ``profile`` fits ``posting`` professionally (0.0–1.0).

    Deterministic and completely independent of eligibility — see the module
    docstring and the README's "Professional match scoring" section for the
    exact formula, weights, and missing-information rules.
    """
    scores: dict[str, float] = {}
    matched: list[str] = []
    partial: list[str] = []
    missing_required: list[str] = []
    missing_preferred: list[str] = []
    experience_matches: list[str] = []
    interview_topics: list[str] = []

    # A skill may be demonstrated through the technologies list and vice versa.
    skill_pool = _dedupe([*profile.skills, *profile.technologies])
    technology_pool = _dedupe([*profile.technologies, *profile.skills])

    # Required skills (weight 0.35): misses are hard gaps.
    if posting.required_skills:
        score, found, near, missing = _score_items(posting.required_skills, skill_pool)
        scores["required_skills"] = score
        matched += found
        partial += near
        missing_required += missing
        interview_topics += [f"Missing required skill: {item}" for item in missing]

    # Preferred / nice-to-have skills (weight 0.10): misses are soft gaps.
    if posting.preferred_skills:
        score, found, near, missing = _score_items(posting.preferred_skills, skill_pool)
        scores["preferred_skills"] = score
        matched += found
        partial += near
        missing_preferred += missing

    # Experience (weight 0.20): candidate years vs. the stated minimum.
    if posting.min_experience_years is not None:
        required_years = float(posting.min_experience_years)
        if profile.years_experience is None:
            scores["experience"] = 0.0
            interview_topics.append("Years of experience not stated in the profile")
        else:
            candidate_years = float(profile.years_experience)
            if required_years <= 0:
                scores["experience"] = 1.0
                experience_matches.append("no minimum experience required")
            elif candidate_years >= required_years:
                scores["experience"] = 1.0
                experience_matches.append(
                    f"{_fmt_years(candidate_years)} years of experience meets the "
                    f"{_fmt_years(required_years)} years required"
                )
            else:
                scores["experience"] = candidate_years / required_years
                interview_topics.append(
                    f"Experience shortfall: {_fmt_years(candidate_years)} of "
                    f"{_fmt_years(required_years)} years required"
                )

    # Technologies / tools (weight 0.15): treated as required tooling; their
    # evidence flows into the skill evidence lists.
    if posting.technologies:
        score, found, near, missing = _score_items(posting.technologies, technology_pool)
        scores["technologies"] = score
        matched += found
        partial += near
        missing_required += missing
        interview_topics += [f"Missing required tool: {item}" for item in missing]

    # Education (weight 0.08): level hierarchy, then item matching.
    if posting.education:
        if not profile.education:
            scores["education"] = 0.0
            interview_topics.append(
                "Education requirement not demonstrated: profile lists no education"
            )
        else:
            item_scores = [
                _education_item_score(item, profile.education)
                for item in posting.education
            ]
            scores["education"] = sum(item_scores) / len(item_scores)
            interview_topics += [
                (
                    f"Education requirement not demonstrated: {item}"
                    if value == 0.0
                    else f"Education requirement only partially demonstrated: {item}"
                )
                for item, value in zip(posting.education, item_scores)
                if value < 1.0
            ]

    # Certifications (weight 0.05): exact/partial item matching.
    if posting.certifications:
        score, _, _, missing = _score_items(posting.certifications, profile.certifications)
        scores["certifications"] = score
        interview_topics += [
            f"Certification requirement not demonstrated: {item}" for item in missing
        ]

    # Languages (weight 0.07): exact/partial item matching.
    if posting.languages:
        score, _, _, missing = _score_items(posting.languages, profile.languages)
        scores["languages"] = score
        interview_topics += [
            f"Language requirement not demonstrated: {item}" for item in missing
        ]

    # ATS keywords derived from the posting's structured requirements.
    cv_keywords = _dedupe(
        [*posting.required_skills, *posting.preferred_skills, *posting.technologies]
    )

    # Final calculation: weighted average over assessed dimensions only.
    if scores:
        assessed_weight = sum(DIMENSION_WEIGHTS[name] for name in scores)
        raw_score = (
            sum(DIMENSION_WEIGHTS[name] * scores[name] for name in scores)
            / assessed_weight
        )
        components = ", ".join(
            f"{name} {DIMENSION_WEIGHTS[name]:.2f}x{scores[name]:.2f}"
            for name in _DIMENSION_ORDER
            if name in scores
        )
        excluded = [name for name in _DIMENSION_ORDER if name not in scores]
        rationale = (
            "score = sum(w*s)/sum(w) over assessed dimensions [" + components + "]"
        )
        if excluded:
            rationale += "; excluded (not stated): [" + ", ".join(excluded) + "]"
    else:
        raw_score = 0.0
        rationale = (
            "no professional requirements stated in the posting; "
            "nothing to assess (score 0.0)"
        )

    # Clamp defensively, then round for stable, readable output.
    score = round(min(1.0, max(0.0, raw_score)), 4)

    return ProfessionalMatch(
        score=score,
        rationale=rationale,
        matched_skills=_dedupe(matched),
        partial_matches=_dedupe(partial),
        missing_required_skills=_dedupe(missing_required),
        missing_preferred_skills=_dedupe(missing_preferred),
        experience_matches=tuple(experience_matches),
        cv_keywords=cv_keywords,
        interview_topics=tuple(interview_topics),
    )
