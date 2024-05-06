import pathlib
from typing import Sequence

import jsonschema
import pytest

from pytest_mypy_plugins.collect import YamlTestFile
from pytest_mypy_plugins.definition import validate_schema


def get_all_yaml_files(dir_path: pathlib.Path) -> Sequence[pathlib.Path]:
    yaml_files = []
    for file in dir_path.rglob("*"):
        if file.suffix in (".yml", ".yaml"):
            yaml_files.append(file)

    return yaml_files


files = get_all_yaml_files(pathlib.Path(__file__).parent)


@pytest.mark.parametrize("yaml_file", files, ids=lambda x: x.stem)
def test_yaml_files(yaml_file: pathlib.Path) -> None:
    validate_schema(YamlTestFile.read_yaml_file(yaml_file))


def test_mypy_config_is_not_an_object() -> None:
    with pytest.raises(jsonschema.exceptions.ValidationError) as ex:
        validate_schema(
            [
                {
                    "__line__": 0,
                    "case": "mypy_config_is_not_an_object",
                    "main": "False",
                    "mypy_config": [{"force_uppercase_builtins": True}, {"force_union_syntax": True}],
                }
            ]
        )

    assert (
        ex.value.message == "[{'force_uppercase_builtins': True}, {'force_union_syntax': True}] is not of type 'string'"
    )


def test_closed_schema() -> None:
    with pytest.raises(jsonschema.exceptions.ValidationError) as ex:
        validate_schema(
            [
                {
                    "__line__": 0,
                    "case": "mypy_config_is_not_an_object",
                    "main": "False",
                    "extra_field": 1,
                }
            ],
            is_closed=True,
        )

    assert ex.value.message == "Additional properties are not allowed ('extra_field' was unexpected)"


def test_files_in_first_run_cant_be_null() -> None:
    with pytest.raises(jsonschema.exceptions.ValidationError) as ex:
        validate_schema(
            [
                {
                    "__line__": 0,
                    "case": "mypy_config_is_not_an_object",
                    "main": "False",
                    "files": [{"path": "a.py", "content": None}],
                }
            ]
        )
    assert ex.value.message == "None is not of type 'string'"
    assert list(ex.value.schema_path) == ["items", "properties", "files", "items", "properties", "content", "type"]


def test_closed_schema_with_followup() -> None:
    validate_schema(
        [
            {
                "__line__": 0,
                "case": "mypy_config_is_not_an_object",
                "main": "False",
                "followups": [
                    {"main": "True"},
                    {"files": [{"path": "a.py", "content": ""}], "out": ""},
                    {"files": [{"path": "a.py", "content": None}], "out": ""},
                ],
            }
        ],
        is_closed=True,
    )
