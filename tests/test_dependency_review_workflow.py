from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_TEXT = (ROOT / ".github/workflows/dependency-review.yml").read_text(
    encoding="utf-8"
)


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=False)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)
WORKFLOW = yaml.load(WORKFLOW_TEXT, Loader=UniqueKeyLoader)


def test_actual_api_availability_controls_the_pinned_review_action() -> None:
    job = WORKFLOW["jobs"]["dependency-review"]
    steps = {step["name"]: step for step in job["steps"]}
    probe = steps["Probe dependency-review availability"]
    unavailable = steps["Explain unavailable dependency-review boundary"]
    review = steps["Review dependency changes"]

    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert probe["id"] == "dependency-review-availability"
    assert "dependency_review_availability.py" in probe["run"]
    assert "github.event.pull_request.base.sha" in probe["run"]
    assert "github.event.pull_request.head.sha" in probe["run"]
    assert unavailable["if"] == (
        "${{ steps.dependency-review-availability.outputs.available == 'false' }}"
    )
    assert review["if"] == (
        "${{ steps.dependency-review-availability.outputs.available == 'true' }}"
    )
    assert review["uses"] == (
        "actions/dependency-review-action@"
        "a1d282b36b6f3519aa1f3fc636f609c47dddb294"
    )
    assert "warn-only" not in review.get("with", {})
    assert "continue-on-error" not in review
