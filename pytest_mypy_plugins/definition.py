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
    Union,
)

import jsonschema
import pytest

from . import utils
from .scenario import MypyPluginsConfig, MypyPluginsScenario


def validate_schema(data: List[Mapping[str, Any]], *, is_closed: bool = False) -> None:
    """Validate the schema of the file-under-test."""
    schema = json.loads((pathlib.Path(__file__).parent / "schema.json").read_text("utf8"))
    schema["items"]["properties"]["__line__"] = {
        "type": "integer",
        "description": "Line number where the test starts (`pytest-mypy-plugins` internal)",
    }
    schema["items"]["additionalProperties"] = not is_closed

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
    starting_lineno: int
    parsed_test_data: Mapping[str, object]
    additional_properties: Mapping[str, object]
    environment_variables: MutableMapping[str, object]

    out: str = ""
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

            # Get the parametrized options
            parametrized = raw_item.pop("parametrized", None)
            if not isinstance(parametrized, list):
                parametrized = []
            parametrized = _parse_parametrized(parametrized)

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
        scenario.expect_fail = self.expect_fail
        scenario.expected_output = self.expected_output

        for file in self.files:
            scenario.make_file(file)

        scenario.run_and_check_mypy("main.py")
