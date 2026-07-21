"""Deterministic initial mathematics curriculum pack (Requirement 12).

This module builds the versioned seed manifest for the initial grades 3-5
fractions, decimals, and percentages Concept_DAG. It defines the exact 15
concepts and prerequisite edges enumerated in Requirement 12 (criteria 12.3
through 12.18) and, for every concept, one short published lesson plus three
varied published questions with answer keys and explanations (Requirement
12.19). Each published version carries an approving reviewer decision so the
pack satisfies the review-before-publication rule (Requirements 12.21, 12.22).

The manifest is produced deterministically: identifiers are derived with
``uuid5`` from stable keys and the checksum is computed from the canonical
payload, so re-applying the pack is idempotent (matching the idempotent
manifest checksum contract already exercised by the seed applier).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.persistence.seeds import CurriculumManifest, manifest_checksum

PACK = "mathematics-fractions"
VERSION = "1"
_NAMESPACE = uuid5(NAMESPACE_URL, f"fastlearner:curriculum:{PACK}")

#: A stable, shared (owner-less) curriculum subject.
SUBJECT_ID = uuid5(_NAMESPACE, "subject:mathematics")
#: A stable curriculum reviewer/administrator account that approves seed content.
REVIEWER_ID = uuid5(_NAMESPACE, "reviewer:curriculum-admin")
REVIEWED_AT = "2024-01-01T00:00:00+00:00"

GRADE_MIN = 3
GRADE_MAX = 5


def _uuid(*parts: str) -> str:
    return str(uuid5(_NAMESPACE, ":".join(parts)))


def _checksum(*fields: Any) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The 15 concepts and their curated content (Requirements 12.3-12.19)
# ---------------------------------------------------------------------------
# Each entry is (key, title, lesson_body, [(prompt, answer_spec, explanation), x3]).

_CONCEPTS: list[dict[str, Any]] = [
    {
        "key": "whole_numbers_and_place_value",
        "title": "Whole Numbers and Place Value",
        "lesson": "Every digit in a whole number has a place value. In 372 the 3 means 3 hundreds, the 7 means 7 tens, and the 2 means 2 ones. Reading places lets us compare and build numbers.",
        "questions": [
            ("What is the value of the digit 5 in the number 258?",
             {"type": "multiple_choice", "choices": ["5", "50", "500"], "correct_index": 1},
             "The 5 is in the tens place, so its value is 5 tens, which is 50."),
            ("Write the number that has 4 hundreds, 0 tens, and 6 ones.",
             {"type": "numeric", "value": 406, "tolerance": 0},
             "4 hundreds is 400, 0 tens is 0, and 6 ones is 6, so the number is 406."),
            ("Which number is greater, 471 or 417?",
             {"type": "exact", "value": "471"},
             "Both have 4 hundreds, but 471 has 7 tens while 417 has only 1 ten, so 471 is greater."),
        ],
    },
    {
        "key": "division_as_sharing",
        "title": "Division as Sharing",
        "lesson": "Division splits a total into equal groups. Sharing 12 apples equally among 3 friends gives 12 / 3 = 4 apples each. The total is shared fairly with nothing left over here.",
        "questions": [
            ("Share 15 stickers equally among 5 children. How many does each child get?",
             {"type": "numeric", "value": 3, "tolerance": 0},
             "15 shared into 5 equal groups is 15 / 5 = 3 stickers each."),
            ("If 20 cookies are shared equally on 4 plates, how many cookies are on each plate?",
             {"type": "multiple_choice", "choices": ["4", "5", "6"], "correct_index": 1},
             "20 / 4 = 5, so each plate holds 5 cookies."),
            ("Which expression means sharing 18 equally among 6?",
             {"type": "exact", "value": "18 / 6"},
             "Sharing 18 into 6 equal groups is written as the division 18 / 6."),
        ],
    },
    {
        "key": "fraction_as_part_of_a_whole",
        "title": "Fraction as Part of a Whole",
        "lesson": "A fraction names equal parts of one whole. If a pizza is cut into 4 equal slices and you take 1 slice, you have one fourth of the pizza, written 1/4.",
        "questions": [
            ("A cake is cut into 8 equal pieces and 3 are eaten. What fraction was eaten?",
             {"type": "fraction", "numerator": 3, "denominator": 8},
             "3 of the 8 equal pieces were eaten, which is the fraction 3/8."),
            ("Which fraction shows one half of a whole?",
             {"type": "multiple_choice", "choices": ["1/4", "1/2", "1/3"], "correct_index": 1},
             "One half means one of two equal parts, written 1/2."),
            ("A bar is split into 5 equal parts. What fraction is one part?",
             {"type": "fraction", "numerator": 1, "denominator": 5},
             "One of five equal parts is 1/5."),
        ],
    },
    {
        "key": "numerator_and_denominator",
        "title": "Numerator and Denominator",
        "lesson": "In a fraction the bottom number, the denominator, tells how many equal parts the whole has. The top number, the numerator, tells how many parts we are counting. In 3/4 the denominator is 4 and the numerator is 3.",
        "questions": [
            ("In the fraction 7/9, what is the denominator?",
             {"type": "numeric", "value": 9, "tolerance": 0},
             "The denominator is the bottom number, which is 9."),
            ("In the fraction 2/5, what is the numerator?",
             {"type": "numeric", "value": 2, "tolerance": 0},
             "The numerator is the top number, which is 2."),
            ("Which part of 4/6 tells how many equal parts the whole is divided into?",
             {"type": "multiple_choice", "choices": ["the 4", "the 6"], "correct_index": 1},
             "The denominator 6 tells how many equal parts make the whole."),
        ],
    },
    {
        "key": "equivalent_fractions",
        "title": "Equivalent Fractions",
        "lesson": "Equivalent fractions name the same amount using different numbers. Multiplying the numerator and denominator by the same number keeps the value: 1/2 = 2/4 = 3/6.",
        "questions": [
            ("Which fraction is equivalent to 1/2?",
             {"type": "multiple_choice", "choices": ["2/3", "2/4", "3/5"], "correct_index": 1},
             "Multiplying 1/2 top and bottom by 2 gives 2/4, so 2/4 equals 1/2."),
            ("Complete the equivalent fraction: 2/3 = ?/9",
             {"type": "numeric", "value": 6, "tolerance": 0},
             "Multiply top and bottom of 2/3 by 3 to get 6/9, so the numerator is 6."),
            ("Is 3/4 equivalent to 6/8?",
             {"type": "exact", "value": "yes"},
             "Multiplying 3/4 top and bottom by 2 gives 6/8, so they are equivalent."),
        ],
    },
    {
        "key": "comparing_fractions",
        "title": "Comparing Fractions",
        "lesson": "To compare fractions, rewrite them with the same denominator, then compare numerators. To compare 1/2 and 2/3 use sixths: 1/2 = 3/6 and 2/3 = 4/6, so 2/3 is larger.",
        "questions": [
            ("Which is greater, 3/5 or 2/5?",
             {"type": "exact", "value": "3/5"},
             "With the same denominator, the larger numerator wins, so 3/5 is greater than 2/5."),
            ("Which is greater, 1/2 or 3/4?",
             {"type": "multiple_choice", "choices": ["1/2", "3/4"], "correct_index": 1},
             "Using fourths, 1/2 = 2/4, which is less than 3/4, so 3/4 is greater."),
            ("Compare 2/3 and 2/5. Which is smaller?",
             {"type": "exact", "value": "2/5"},
             "With equal numerators, the larger denominator makes smaller parts, so 2/5 is smaller."),
        ],
    },
    {
        "key": "simplifying_fractions",
        "title": "Simplifying Fractions",
        "lesson": "A fraction is in simplest form when the numerator and denominator share no common factor except 1. Divide both by their greatest common factor: 4/8 divides by 4 to give 1/2.",
        "questions": [
            ("Write 6/8 in simplest form.",
             {"type": "fraction", "numerator": 3, "denominator": 4},
             "The greatest common factor of 6 and 8 is 2, so 6/8 = 3/4."),
            ("Write 10/15 in simplest form.",
             {"type": "fraction", "numerator": 2, "denominator": 3},
             "Dividing 10 and 15 by 5 gives 2/3."),
            ("Is 5/9 already in simplest form?",
             {"type": "exact", "value": "yes"},
             "5 and 9 share no common factor greater than 1, so 5/9 is already simplest."),
        ],
    },
    {
        "key": "addition_and_subtraction_with_like_denominators",
        "title": "Addition and Subtraction with Like Denominators",
        "lesson": "When fractions have the same denominator, add or subtract the numerators and keep the denominator. 2/7 + 3/7 = 5/7 and 5/7 - 1/7 = 4/7.",
        "questions": [
            ("What is 1/5 + 2/5?",
             {"type": "fraction", "numerator": 3, "denominator": 5},
             "Add the numerators 1 + 2 = 3 and keep the denominator 5 to get 3/5."),
            ("What is 5/8 - 2/8?",
             {"type": "fraction", "numerator": 3, "denominator": 8},
             "Subtract the numerators 5 - 2 = 3 and keep the denominator 8 to get 3/8."),
            ("When adding fractions with the same denominator, what stays the same?",
             {"type": "multiple_choice", "choices": ["the numerator", "the denominator"], "correct_index": 1},
             "The denominator stays the same; only the numerators are added."),
        ],
    },
    {
        "key": "addition_and_subtraction_with_unlike_denominators",
        "title": "Addition and Subtraction with Unlike Denominators",
        "lesson": "To add fractions with different denominators, first rewrite them as equivalent fractions with a common denominator, then add the numerators. 1/2 + 1/3 = 3/6 + 2/6 = 5/6.",
        "questions": [
            ("What is 1/2 + 1/4?",
             {"type": "fraction", "numerator": 3, "denominator": 4},
             "Rewrite 1/2 as 2/4, then 2/4 + 1/4 = 3/4."),
            ("What is 2/3 - 1/6?",
             {"type": "fraction", "numerator": 1, "denominator": 2},
             "Rewrite 2/3 as 4/6, then 4/6 - 1/6 = 3/6 = 1/2."),
            ("What is the first step to add 1/3 and 1/4?",
             {"type": "multiple_choice",
              "choices": ["add the numerators", "find a common denominator"],
              "correct_index": 1},
             "You must first rewrite both with a common denominator before adding."),
        ],
    },
    {
        "key": "multiplication_of_fractions",
        "title": "Multiplication of Fractions",
        "lesson": "To multiply fractions, multiply the numerators together and the denominators together. 2/3 x 4/5 = (2x4)/(3x5) = 8/15.",
        "questions": [
            ("What is 1/2 x 1/3?",
             {"type": "fraction", "numerator": 1, "denominator": 6},
             "Multiply numerators 1x1 = 1 and denominators 2x3 = 6 to get 1/6."),
            ("What is 2/3 x 3/4?",
             {"type": "fraction", "numerator": 1, "denominator": 2},
             "Multiply to get 6/12, which simplifies to 1/2."),
            ("To multiply two fractions you multiply the numerators and the ___.",
             {"type": "exact", "value": "denominators"},
             "Multiplying fractions multiplies numerators together and denominators together."),
        ],
    },
    {
        "key": "division_of_fractions",
        "title": "Division of Fractions",
        "lesson": "To divide by a fraction, multiply by its reciprocal (flip the second fraction). 1/2 / 1/4 = 1/2 x 4/1 = 4/2 = 2.",
        "questions": [
            ("What is 1/2 / 1/4?",
             {"type": "numeric", "value": 2, "tolerance": 0},
             "Multiply 1/2 by the reciprocal 4/1 to get 4/2 = 2."),
            ("What is the reciprocal of 3/5?",
             {"type": "fraction", "numerator": 5, "denominator": 3},
             "The reciprocal flips the fraction, so 3/5 becomes 5/3."),
            ("Dividing by a fraction is the same as multiplying by its ___.",
             {"type": "exact", "value": "reciprocal"},
             "To divide by a fraction, multiply by its reciprocal."),
        ],
    },
    {
        "key": "decimal_place_value",
        "title": "Decimal Place Value",
        "lesson": "Digits after a decimal point show parts smaller than one. In 0.36 the 3 is 3 tenths and the 6 is 6 hundredths. Place value continues to the right of the point.",
        "questions": [
            ("What is the value of the digit 7 in 0.7?",
             {"type": "multiple_choice", "choices": ["7 ones", "7 tenths", "7 hundredths"], "correct_index": 1},
             "The first place after the decimal point is tenths, so 7 means 7 tenths."),
            ("Write 'four tenths' as a decimal.",
             {"type": "exact", "value": "0.4"},
             "Four tenths is written as 0.4."),
            ("In 0.25, which digit is in the hundredths place?",
             {"type": "numeric", "value": 5, "tolerance": 0},
             "The second place after the decimal point is hundredths, where the digit is 5."),
        ],
    },
    {
        "key": "fractions_as_decimals",
        "title": "Fractions as Decimals",
        "lesson": "A fraction can be written as a decimal by finding an equivalent fraction with denominator 10, 100, and so on, or by dividing. 1/2 = 5/10 = 0.5 and 1/4 = 25/100 = 0.25.",
        "questions": [
            ("Write 1/2 as a decimal.",
             {"type": "exact", "value": "0.5"},
             "1/2 equals 5/10, which is 0.5."),
            ("Write 3/4 as a decimal.",
             {"type": "exact", "value": "0.75"},
             "3/4 equals 75/100, which is 0.75."),
            ("Write 1/10 as a decimal.",
             {"type": "exact", "value": "0.1"},
             "1/10 is one tenth, written 0.1."),
        ],
    },
    {
        "key": "decimals_as_percentages",
        "title": "Decimals as Percentages",
        "lesson": "Percent means 'per hundred'. To turn a decimal into a percentage, multiply by 100 and add the percent sign. 0.5 = 50% and 0.07 = 7%.",
        "questions": [
            ("Write 0.5 as a percentage.",
             {"type": "exact", "value": "50%"},
             "Multiply 0.5 by 100 to get 50, so 0.5 = 50%."),
            ("Write 0.25 as a percentage.",
             {"type": "exact", "value": "25%"},
             "0.25 x 100 = 25, so 0.25 = 25%."),
            ("To change a decimal to a percentage you multiply by ___.",
             {"type": "numeric", "value": 100, "tolerance": 0},
             "Multiplying a decimal by 100 converts it to a percentage."),
        ],
    },
    {
        "key": "percentage_of_a_quantity",
        "title": "Percentage of a Quantity",
        "lesson": "To find a percentage of a quantity, write the percent as a fraction or decimal and multiply. 50% of 20 is 0.5 x 20 = 10, and 10% of 40 is 0.1 x 40 = 4.",
        "questions": [
            ("What is 50% of 20?",
             {"type": "numeric", "value": 10, "tolerance": 0},
             "50% is 0.5, and 0.5 x 20 = 10."),
            ("What is 10% of 90?",
             {"type": "numeric", "value": 9, "tolerance": 0},
             "10% is 0.1, and 0.1 x 90 = 9."),
            ("What is 25% of 8?",
             {"type": "numeric", "value": 2, "tolerance": 0},
             "25% is 0.25, and 0.25 x 8 = 2."),
        ],
    },
]

#: Prerequisite edges as (concept_key, prerequisite_key) exactly per Requirement 12.
_EDGES: list[tuple[str, str]] = [
    ("division_as_sharing", "whole_numbers_and_place_value"),
    ("fraction_as_part_of_a_whole", "division_as_sharing"),
    ("numerator_and_denominator", "fraction_as_part_of_a_whole"),
    ("equivalent_fractions", "numerator_and_denominator"),
    ("comparing_fractions", "equivalent_fractions"),
    ("simplifying_fractions", "equivalent_fractions"),
    ("addition_and_subtraction_with_like_denominators", "numerator_and_denominator"),
    ("addition_and_subtraction_with_unlike_denominators", "equivalent_fractions"),
    ("addition_and_subtraction_with_unlike_denominators", "addition_and_subtraction_with_like_denominators"),
    ("multiplication_of_fractions", "numerator_and_denominator"),
    ("division_of_fractions", "multiplication_of_fractions"),
    ("decimal_place_value", "whole_numbers_and_place_value"),
    ("fractions_as_decimals", "equivalent_fractions"),
    ("fractions_as_decimals", "decimal_place_value"),
    ("decimals_as_percentages", "fractions_as_decimals"),
    ("percentage_of_a_quantity", "decimals_as_percentages"),
]


def _concept_id(key: str) -> str:
    return _uuid("concept", key)


def build_payload() -> dict[str, Any]:
    """Build the deterministic mathematics pack manifest payload."""
    concepts_payload: list[dict[str, Any]] = []
    content_items_payload: list[dict[str, Any]] = []
    question_versions_payload: list[dict[str, Any]] = []
    reviews_payload: list[dict[str, Any]] = []

    for concept in _CONCEPTS:
        key = concept["key"]
        concept_id = _concept_id(key)
        concepts_payload.append(
            {
                "id": concept_id,
                "key": key,
                "title": concept["title"],
                "grade_min": GRADE_MIN,
                "grade_max": GRADE_MAX,
                "status": "published",
                "version": 1,
            }
        )

        # One short published lesson per concept (Requirement 12.19).
        lesson_id = _uuid("content", key, "lesson", "1")
        lesson_title = f"{concept['title']} Lesson"
        lesson_body = concept["lesson"]
        content_items_payload.append(
            {
                "id": lesson_id,
                "concept_id": concept_id,
                "kind": "lesson",
                "version": 1,
                "status": "published",
                "title": lesson_title,
                "body": lesson_body,
                "checksum": _checksum("lesson", key, 1, lesson_title, lesson_body),
            }
        )
        reviews_payload.append(
            {
                "id": _uuid("review", "content", lesson_id),
                "content_item_id": lesson_id,
                "question_version_id": None,
                "reviewer_user_id": str(REVIEWER_ID),
                "decision": "approved",
                "notes": "Curated seed content approved for publication.",
                "reviewed_at": REVIEWED_AT,
            }
        )

        # Three varied published questions with answers and explanations.
        for index, (prompt, answer_spec, explanation) in enumerate(concept["questions"], start=1):
            question_key = f"{key}_q{index}"
            question_id = _uuid("question", question_key, "1")
            provenance = {
                "origin": "curated",
                "pack": PACK,
                "pack_version": VERSION,
                "author": "fastlearner-curriculum",
            }
            question_versions_payload.append(
                {
                    "id": question_id,
                    "concept_id": concept_id,
                    "question_key": question_key,
                    "version": 1,
                    "status": "published",
                    "prompt": prompt,
                    "answer_spec": answer_spec,
                    "explanation": explanation,
                    "provenance": provenance,
                    "checksum": _checksum("question", question_key, 1, prompt, answer_spec, explanation),
                }
            )
            reviews_payload.append(
                {
                    "id": _uuid("review", "question", question_id),
                    "content_item_id": None,
                    "question_version_id": question_id,
                    "reviewer_user_id": str(REVIEWER_ID),
                    "decision": "approved",
                    "notes": "Curated seed question approved for publication.",
                    "reviewed_at": REVIEWED_AT,
                }
            )

    edges_payload = [
        {
            "id": _uuid("edge", concept_key, prerequisite_key),
            "concept_id": _concept_id(concept_key),
            "prerequisite_concept_id": _concept_id(prerequisite_key),
        }
        for concept_key, prerequisite_key in _EDGES
    ]

    return {
        "subject": {
            "id": str(SUBJECT_ID),
            "owner_user_id": None,
            "slug": "mathematics",
            "title": "Mathematics: Fractions, Decimals, and Percentages",
            "kind": "curriculum",
            "status": "active",
            "archived_at": None,
        },
        "reviewers": [
            {
                "id": str(REVIEWER_ID),
                "email": "curriculum-reviewer@local.fastlearner",
                "display_name": "Curriculum Reviewer",
                "status": "active",
            }
        ],
        "concepts": concepts_payload,
        "edges": edges_payload,
        "content_items": content_items_payload,
        "question_versions": question_versions_payload,
        "reviews": reviews_payload,
    }


def mathematics_manifest() -> CurriculumManifest:
    """Return the initial mathematics pack as a checksummed manifest."""
    payload = build_payload()
    return CurriculumManifest(
        pack=PACK,
        version=VERSION,
        payload=payload,
        checksum=manifest_checksum(payload),
    )
