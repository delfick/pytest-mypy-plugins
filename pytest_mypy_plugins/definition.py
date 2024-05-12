import dataclasses
import json
import os
import pathlib
import platform
import sys
from collections import defaultdict
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    MutableSequence,
    Sequence,
    Tuple,
    Union,
)

import jsonschema
import pytest

from . import utils
from .scenario import Followup, MypyPluginsConfig, MypyPluginsScenario


def validate_schema(data: List[Mapping[str, Any]], *, is_closed: bool = False) -> None:
    """Validate the schema of the file-under-test."""
    schema = json.loads((pathlib.Path(__file__).parent / "schema.json").read_text("utf8"))
    schema["items"]["properties"]["__line__"] = {
        "type": "integer",
        "description": "Line number where the test starts (`pytest-mypy-plugins` internal)",
    }
    schema["items"]["additionalProperties"] = not is_closed
    schema["definitions"]["Followup"]["additionalProperties"] = not is_closed

    jsonschema.validate(instance=data, schema=schema)


def _parse_test_files(files: List[Mapping[str, str]]) -> List[utils.File]:
    return [
        utils.File(
            path=file["path"],
            **({} if "content" not in file else {"content": file["content"]}),
        )
        for file in files
    ]


def _parse_environment_variables(env_vars: List[str]) -> Mapping[str, str]:
    parsed_vars: Dict[str, str] = {}
    for env_var in env_vars:
        name, _, value = env_var.partition("=")
        parsed_vars[name] = value
    return parsed_vars


def _parse_parametrized(params: List[Mapping[str, object]]) -> Iterator[Mapping[str, object]]:
    if not params:
        yield {}
        return

    by_keys: Mapping[str, List[Mapping[str, object]]] = defaultdict(list)
    for idx, param in enumerate(params):
        keys = ", ".join(sorted(param))
        if by_keys and keys not in by_keys:
            raise ValueError(
                "All parametrized entries must have same keys."
                f'First entry is {", ".join(sorted(list(by_keys)[0]))} but {keys} '
                "was spotted at {idx} position",
            )

        by_keys[keys].append({k: v for k, v in param.items() if not k.startswith("__")})

    if len(by_keys) != 1:
        # This should never happen and is a defensive repetition of the above error
        raise ValueError("All parametrized entries must have the same keys")

    for param_lists in by_keys.values():
        yield from param_lists


def _parse_followups(followups: List[Mapping[str, object]]) -> Iterator[Followup]:
    """
    followups is assumed to be after the schema has been validated on the list
    """
    fields = [f.name for f in dataclasses.fields(Followup)]
    file_fields = [f.name for f in dataclasses.fields(utils.FollowupFile)]

    for followup in followups:
        kwargs: Dict[str, Any] = {}

        additional_properties: Dict[str, object] = {}
        kwargs["additional_properties"] = additional_properties
        found_additional = followup.get("additional_properties")
        if isinstance(found_additional, Mapping):
            kwargs["additional_properties"].update(found_additional)

        for k, v in followup.items():
            if k in fields:
                kwargs[k] = v
            else:
                additional_properties[k] = v

        files = kwargs.pop("files", [])
        kwargs["files"] = []
        for file in files:
            options: Dict[str, Any] = {k: v for k, v in file.items() if k in file_fields}
            kwargs["files"].append(utils.FollowupFile(**options))

        yield Followup(**kwargs)


def _run_skip(skip: Union[bool, str]) -> bool:
    if isinstance(skip, bool):
        return skip
    elif skip == "True":
        return True
    elif skip == "False":
        return False
    else:
        return eval(skip, {"sys": sys, "os": os, "pytest": pytest, "platform": platform})


def _create_output_matchers(
    *, regex: bool, files: Sequence[utils.File], out: str, params: Mapping[str, object]
) -> MutableSequence[utils.OutputMatcher]:
    expected_output: List[utils.OutputMatcher] = []
    for test_file in files:
        output_lines = utils.extract_output_matchers_from_comments(
            test_file.path, test_file.content.split("\n"), regex=regex
        )
        expected_output.extend(output_lines)

    expected_output.extend(utils.extract_output_matchers_from_out(out, params, regex=regex))
    return expected_output


@dataclasses.dataclass
class ItemDefinition:
    """
    A dataclass representing a single test in the yaml file
    """

    case: str
    main: str
    files: MutableSequence[utils.File]
    followups: MutableSequence[Followup]
    starting_lineno: int
    parsed_test_data: Mapping[str, object]
    additional_properties: Mapping[str, object]
    environment_variables: MutableMapping[str, object]

    out: str = ""
    start: List[str] = dataclasses.field(default_factory=lambda: ["main.py"])
    skip: Union[bool, str] = False
    regex: bool = False
    mypy_config: str = ""
    expect_fail: bool = False
    disable_cache: bool = False

    # These are set when `from_yaml` returns all the parametrized, non skipped tests
    item_params: Mapping[str, object] = dataclasses.field(default_factory=dict, init=False)
    additional_mypy_config: str = dataclasses.field(init=False)
    expected_output: MutableSequence[utils.OutputMatcher] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if not self.case.isidentifier():
            raise ValueError(f"Invalid test name {self.case!r}, only '[a-zA-Z0-9_]' is allowed.")

    @classmethod
    def from_yaml(cls, data: List[Mapping[str, object]], *, is_closed: bool = False) -> Iterator["ItemDefinition"]:
        # Validate the shape of data so we can make reasonable assumptions
        validate_schema(data, is_closed=is_closed)

        for _raw_item in data:
            raw_item = dict(_raw_item)

            additional_properties: Dict[str, object] = {}
            kwargs: Dict[str, Any] = {
                "parsed_test_data": _raw_item,
                "additional_properties": additional_properties,
            }

            fields = [f.name for f in dataclasses.fields(cls)]

            # Convert the injected __line__ into starting_lineno
            starting_lineno = raw_item["__line__"]
            if not isinstance(starting_lineno, int):
                raise RuntimeError("__line__ should have been set as an integer")
            kwargs["starting_lineno"] = starting_lineno

            # Make sure we have a list of File objects for files
            files = raw_item.pop("files", None)
            if not isinstance(files, list):
                files = []
            kwargs["files"] = _parse_test_files(files)

            # Get our extra environment variables
            env = raw_item.pop("env", None)
            if not isinstance(env, list):
                env = []
            kwargs["environment_variables"] = _parse_environment_variables(env)

            # Get any followup options
            followups = raw_item.pop("followups", None)
            if not isinstance(followups, list):
                followups = []
            kwargs["followups"] = list(_parse_followups(followups))

            # make sure start is a list of strings
            if isinstance(raw_item.get("start"), str):
                kwargs["start"] = [raw_item.pop("start")]

            # Get the parametrized options
            parametrized = raw_item.pop("parametrized", None)
            if not isinstance(parametrized, list):
                parametrized = []
            parametrized = list(_parse_parametrized(parametrized))

            # Set the rest of the options
            for k, v in raw_item.items():
                if k in fields:
                    kwargs[k] = v
                else:
                    additional_properties[k] = v

            nxt = cls(**kwargs)
            for params in parametrized:
                clone = nxt.clone(params)

                if not _run_skip(clone.skip):
                    yield clone

    def clone(self, item_params: Mapping[str, object]) -> "ItemDefinition":
        clone = dataclasses.replace(self)
        clone.files = list(clone.files)
        clone.environment_variables = dict(clone.environment_variables)
        clone.item_params = item_params
        clone.additional_mypy_config = utils.render_template(template=self.mypy_config, data=item_params)
        clone.expected_output = _create_output_matchers(
            regex=clone.regex, files=[clone.main_file, *clone.files], out=clone.out, params=item_params
        )
        return clone

    @property
    def test_name(self) -> str:
        test_name_prefix = self.case

        test_name_suffix = ""
        if self.item_params:
            test_name_suffix = ",".join(f"{k}={v}" for k, v in self.item_params.items())
            test_name_suffix = f"[{test_name_suffix}]"

        return f"{test_name_prefix}{test_name_suffix}"

    @property
    def main_file(self) -> utils.File:
        content = utils.render_template(template=self.main, data=self.item_params)
        return utils.File(path="main.py", content=content)

    def runtest(self, mypy_plugins_config: MypyPluginsConfig, mypy_plugins_scenario: MypyPluginsScenario) -> None:
        scenario = mypy_plugins_scenario

        # Ensure main_file is available to the extension_hook
        self.files.insert(0, self.main_file)

        # extension point for derived packages
        mypy_plugins_config.execute_extension_hook(self)

        scenario.disable_cache = self.disable_cache
        scenario.environment_variables = self.environment_variables
        scenario.additional_mypy_config = self.additional_mypy_config

        files: MutableMapping[str, str] = {}
        for file in self.files:
            scenario.make_file(file)
            files[file.path] = file.content

        out = self.out
        expect_fail = self.expect_fail
        expected_output = self.expected_output

        scenario.run_and_check_mypy(
            self.start,
            expect_fail=expect_fail,
            expected_output=expected_output,
            additional_properties=self.additional_properties,
        )

        for idx, followup in enumerate(self.followups):
            if not _run_skip(followup.skip):
                scenario.runs.append(f"Running followup: {idx}: {followup.description}")
                expect_fail, out, expected_output = self.followup(
                    scenario,
                    followup,
                    files=files,
                    previous_out=out,
                    previous_expect_fail=expect_fail,
                    previous_expected_output=expected_output,
                )
            else:
                scenario.runs.append(f"Skipping followup: {idx}: {followup.description}")

    def followup(
        self,
        scenario: MypyPluginsScenario,
        followup: Followup,
        files: MutableMapping[str, str],
        previous_out: str,
        previous_expect_fail: bool,
        previous_expected_output: MutableSequence[utils.OutputMatcher],
    ) -> Tuple[bool, str, MutableSequence[utils.OutputMatcher]]:
        if followup.main is not None:
            content = utils.render_template(template=followup.main, data=self.item_params)
            scenario.make_file(utils.File(path="main.py", content=content))
            files["main.py"] = content

        for file in followup.files:
            scenario.handle_followup_file(file)
            if file.content:
                files[file.path] = file.content
            elif file.path in files:
                del files[file.path]

        out = previous_out
        if followup.out is not None:
            out = followup.out

        expect_fail = previous_expect_fail
        expected_output = previous_expected_output
        if followup.expect_fail is not None:
            expect_fail = followup.expect_fail

        expected_output = _create_output_matchers(
            regex=self.regex,
            files=[utils.File(path=path, content=content) for path, content in files.items()],
            out=out,
            params=self.item_params,
        )

        scenario.run_and_check_mypy(
            self.start,
            expect_fail=expect_fail,
            expected_output=expected_output,
            additional_properties=followup.additional_properties,
        )
        return expect_fail, out, expected_output
