import copy
import json

import pytest

from review_schema import (REVIEW_LIMITS, ReviewValidationError,
                           review_allowed_refs, review_prompt, validate_review_output)


def evidence():
    return {'segments': [{'id': 'S0001', 'text': 'Supply 供给 Q = 10 + 2P 😀',
                          'start': 0, 'end': 12, 'version': 0, 'audio_seq': 1}],
            'slides': [{'id': 'P0001', 'title': '供给', 'start': 0, 'end': 12,
                        'version': 1, 'sha256': 'a' * 64, 'path': 'slides/private.png'}],
            'source_ids': ['S0001', 'P0001', 'P9999']}


def options():
    return {'card_types': ['concept', 'derivation', 'example', 'pitfall'], 'english': True}


def result():
    return {'schema_version': 1,
            'cards': [{'type': 'concept', 'title': '供给 Supply 😀', 'front': 'What is supply?',
                       'back': '给定价格下的供给量。', 'supplement': 'AI 补充',
                       'english': 'An increase in supply shifts the supply curve to the right.',
                       'english_meaning': '供给增加使供给曲线右移。',
                       'source_ids': ['S0001', 'P0001'], 'missing': ['没有完整推导']}],
            'notes': [{'section': 'structure', 'title': '供给变化', 'body': 'Q = 10 + 2P',
                       'supplement': '', 'source_ids': ['S0001'], 'missing': []}]}


def validate(value):
    return validate_review_output(json.dumps(value, ensure_ascii=False), evidence(), options())


def test_valid_output_is_unicode_preserving_and_does_not_mutate():
    data = result()
    original = copy.deepcopy(data)
    assert validate(data) == data
    assert data == original


def test_refs_come_from_actual_snapshot_not_source_ids_claim():
    assert review_allowed_refs(evidence()) == {'S0001', 'P0001'}
    value = result()
    value['cards'][0]['source_ids'] = ['P9999']
    with pytest.raises(ReviewValidationError):
        validate(value)


@pytest.mark.parametrize('raw', ['', '{', '```json\n{}\n```', '[]', 'null', '{"schema_version":1,"schema_version":1,"cards":[],"notes":[]}'])
def test_malformed_or_duplicate_key_json_never_becomes_complete(raw):
    with pytest.raises(ReviewValidationError):
        validate_review_output(raw, evidence(), options())


@pytest.mark.parametrize('field,value', [('type', 'made-up'), ('front', '  '), ('back', None),
                                       ('source_ids', []), ('source_ids', ['S0001', 'S0001']),
                                       ('missing', 'not-an-array'), ('unknown', 'extra')])
def test_invalid_card_fields_are_rejected(field, value):
    data = result()
    data['cards'][0][field] = value
    with pytest.raises(ReviewValidationError):
        validate(data)


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_length_boundary_counts_unicode_codepoints(offset):
    data = result()
    data['cards'][0]['front'] = '中😀x' * ((REVIEW_LIMITS['front'] + offset) // 3) + '中' * ((REVIEW_LIMITS['front'] + offset) % 3)
    if offset > 0:
        with pytest.raises(ReviewValidationError):
            validate(data)
    else:
        assert len(validate(data)['cards'][0]['front']) == REVIEW_LIMITS['front'] + offset


def test_disabled_english_and_unrequested_types_cannot_slip_in():
    for settings in ({'card_types': ['example'], 'english': True},
                     {'card_types': ['concept'], 'english': False}):
        with pytest.raises(ReviewValidationError):
            validate_review_output(json.dumps(result()), evidence(), settings)


def test_same_source_supports_two_different_cards_but_exact_duplicates_deduplicate():
    data = result()
    data['cards'].append(copy.deepcopy(data['cards'][0]))
    data['cards'].append({**data['cards'][0], 'front': 'Which way does the curve shift?'})
    assert len(validate(data)['cards']) == 2


def test_no_empty_success_and_strict_note_sections():
    with pytest.raises(ReviewValidationError):
        validate({'schema_version': 1, 'cards': [], 'notes': []})
    data = result()
    data['notes'][0]['section'] = 'invented'
    with pytest.raises(ReviewValidationError):
        validate(data)


def test_cards_without_notes_or_forged_inline_citations_are_not_complete():
    data = result()
    data['notes'] = []
    with pytest.raises(ReviewValidationError):
        validate(data)
    data = result()
    data['cards'][0]['back'] = '引用不存在的公式 [S9999]'
    with pytest.raises(ReviewValidationError):
        validate(data)


def test_prompt_uses_only_frozen_public_materials_no_absolute_paths_or_runtime_fields():
    source = evidence()
    source['credential'] = 'DO-NOT-SEND'
    source['slides'][0]['path'] = 'C:/private/account-secret.png'
    prompt = review_prompt(source, options())
    assert 'Supply 供给' in prompt and 'P0001' in prompt
    assert 'C:/private' not in prompt and 'DO-NOT-SEND' not in prompt
    assert 'P9999' not in prompt
    assert '补充' in prompt and '不' in prompt


@pytest.mark.parametrize('collection', ['cards', 'notes'])
def test_missing_material_explanation_cannot_forge_source_references(collection):
    data = result()
    data[collection][0]['missing'] = ['请看不存在的课堂来源 [S9999]']
    with pytest.raises(ReviewValidationError):
        validate(data)
