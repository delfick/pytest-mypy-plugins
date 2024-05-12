import pathlib
from typing import TYPE_CHECKING, Mapping, MutableSequence

from pytest_mypy_plugins import (
    File,
    MypyPluginsScenario,
    OutputMatcher,
    ScenarioHookMaker,
    ScenarioHooks,
    ScenarioHooksRunAndCheckOptions,
)


class Hooks(ScenarioHooks):
    def before_run_and_check_mypy(
        self,
        *,
        scenario: MypyPluginsScenario,
        options: ScenarioHooksRunAndCheckOptions,
        config_file: pathlib.Path,
        expected_output: MutableSequence[OutputMatcher],
        additional_properties: Mapping[str, object],
    ) -> ScenarioHooksRunAndCheckOptions:
        if options.start == "stuff.py":
            scenario.make_file(File(path="stuff.py", content=f"reveal_type({additional_properties['desired_val']})"))
        return options


if TYPE_CHECKING:
    _sh: ScenarioHookMaker = Hooks
