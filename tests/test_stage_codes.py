from app.services.stage_codes import expand_stage_codes, stages_overlap, new_validation_errors


def test_explicit_ranges_include_individual_stages():
    assert {'V5', 'V6', 'V7', 'V8'} <= expand_stage_codes(['V5-V8'])
    assert stages_overlap('V5-V8', ['V6', 'V7', 'V8'])
    assert not stages_overlap('V2-V4', ['V6'])


def test_missing_r6_is_not_inferred_from_r5():
    assert not stages_overlap('R5', ['R6'])
    assert 'R6' not in expand_stage_codes(['R5', 'R7'])


def test_existing_errors_do_not_block_independent_edits_but_new_errors_do():
    assert new_validation_errors(['missing R6'], ['missing R6']) == []
    assert new_validation_errors(['missing R6'], []) == []
    assert new_validation_errors(['missing R6'], ['missing R6', 'invalid threshold']) == ['invalid threshold']
    assert new_validation_errors(['missing R6'], ['missing R7']) == ['missing R7']
