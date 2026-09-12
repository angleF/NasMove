from nasmove.core.states import ConflictPolicy


def test_conflict_policy_exposes_all_supported_strategies() -> None:
    assert {policy.value for policy in ConflictPolicy} == {
        "keep_both",
        "overwrite",
        "skip",
        "overwrite_if_newer",
        "ask",
    }
    assert ConflictPolicy.AUTO_RENAME is ConflictPolicy.KEEP_BOTH
