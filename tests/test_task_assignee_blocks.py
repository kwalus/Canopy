from canopy.core.tasks import parse_task_blocks


def test_task_block_parses_multiple_assignees_from_assignee_line() -> None:
    specs = parse_task_blocks(
        """[task]
title: Coordinate demo prep
description: Split the remaining review tasks.
assignee: @Forge_McClaw, @Gene_McClaw
priority: high
[/task]"""
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec.assignee == "Forge_McClaw"
    assert spec.assignees == ["Forge_McClaw", "Gene_McClaw"]
    assert spec.to_dict()["assignees"] == ["Forge_McClaw", "Gene_McClaw"]


def test_task_block_accepts_explicit_assignees_field() -> None:
    specs = parse_task_blocks(
        """[task]
title: Validate assignment rendering
description: Confirm the rendered card reflects selected agents.
assignees: @Codex_Agent, @Jensen_McClaw
status: open
[/task]"""
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec.assignee == "Codex_Agent"
    assert spec.assignees == ["Codex_Agent", "Jensen_McClaw"]
