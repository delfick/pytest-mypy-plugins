from typing import TYPE_CHECKING

from pytest_mypy_plugins import ExtensionHook, ItemForHook


def hook(item: ItemForHook) -> None:
    parsed_test_data = item.parsed_test_data
    obj_to_reveal = parsed_test_data.get("reveal_type")
    if obj_to_reveal:
        for file in item.files:
            if file.path.endswith("main.py"):
                file.content = f"reveal_type({obj_to_reveal})"


if TYPE_CHECKING:
    _h: ExtensionHook = hook
