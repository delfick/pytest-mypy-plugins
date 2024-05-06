import os
import pathlib
import shutil
import tempfile
import textwrap
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Hashable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)

import pytest
import yaml
from _pytest._code import ExceptionInfo
from _pytest._code.code import ReprEntry, ReprFileLocation, TerminalRepr
from _pytest._io import TerminalWriter
from _pytest.config.argparsing import Parser
from _pytest.nodes import Node

from . import utils
from .definition import ItemDefinition
from .scenario import MypyPluginsConfig, MypyPluginsScenario, Strategy

# For backwards compatibility reasons this reference stays here
File = utils.File

if TYPE_CHECKING:
    from _pytest._code.code import _TracebackStyle


class TraceLastReprEntry(ReprEntry):
    def toterminal(self, tw: TerminalWriter) -> None:
        if not self.reprfileloc:
            return

        self.reprfileloc.toterminal(tw)
        for line in self.lines:
            red = line.startswith("E   ")
            tw.line(line, bold=True, red=red)
        return


class SafeLineLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> Dict[Hashable, Any]:
        mapping = super().construct_mapping(node, deep=deep)
        # Add 1 so line numbering starts at 1
        starting_line = node.start_mark.line + 1
        for title_node, _contents_node in node.value:
            if title_node.value == "main":
                starting_line = title_node.start_mark.line + 1
        mapping["__line__"] = starting_line
        return mapping


class YamlTestItem(pytest.Function):
    def __init__(
        self,
        name: str,
        parent: pytest.Collector,
        *,
        callobj: Callable[..., None],
        starting_lineno: int,
        originalname: Optional[str] = None,
    ) -> None:
        super().__init__(name, parent, callobj=callobj, originalname=originalname)
        self.starting_lineno = starting_lineno

    def repr_failure(
        self, excinfo: ExceptionInfo[BaseException], style: Optional["_TracebackStyle"] = None
    ) -> Union[str, TerminalRepr]:
        if isinstance(excinfo.value, SystemExit):
            # We assume that before doing exit() (which raises SystemExit) we've printed
            # enough context about what happened so that a stack trace is not useful.
            # In particular, uncaught exceptions during semantic analysis or type checking
            # call exit() and they already print out a stack trace.
            return excinfo.exconly(tryshort=True)
        elif isinstance(excinfo.value, utils.TypecheckAssertionError):
            # with traceback removed
            exception_repr = excinfo.getrepr(style="short")
            exception_repr.reprcrash.message = ""  # type: ignore
            repr_file_location = ReprFileLocation(
                path=str(self.path), lineno=self.starting_lineno + excinfo.value.lineno, message=""
            )
            repr_tb_entry = TraceLastReprEntry(
                exception_repr.reprtraceback.reprentries[-1].lines[1:], None, None, repr_file_location, "short"
            )
            exception_repr.reprtraceback.reprentries = [repr_tb_entry]
            return exception_repr
        else:
            return super(pytest.Function, self).repr_failure(excinfo, style="native")

    def reportinfo(self) -> Tuple[Union[Path, str], Optional[int], str]:
        return self.path, None, self.name


class YamlTestFile(pytest.File):
    @classmethod
    def read_yaml_file(cls, path: pathlib.Path) -> List[Mapping[str, Any]]:
        parsed_file = yaml.load(stream=path.read_text("utf8"), Loader=SafeLineLoader)
        if parsed_file is None:
            return []

        # Unfortunately, yaml.safe_load() returns Any,
        # so we make our intention explicit here.
        if not isinstance(parsed_file, list):
            raise ValueError(f"Test file has to be YAML list, got {type(parsed_file)!r}.")

        return parsed_file

    def collect(self) -> Iterator[pytest.Item]:
        is_closed = self.config.option.mypy_closed_schema
        parsed_file = self.read_yaml_file(self.path)

        for test in ItemDefinition.from_yaml(parsed_file, is_closed=is_closed):
            yield YamlTestItem.from_parent(
                self,
                name=test.test_name,
                callobj=test.runtest,
                originalname=test.case,
                starting_lineno=test.starting_lineno,
            )


@pytest.fixture(scope="session")
def mypy_plugins_config(pytestconfig: pytest.Config) -> MypyPluginsConfig:
    mypy_executable = shutil.which("mypy")
    assert mypy_executable is not None, "mypy executable is not found"

    return MypyPluginsConfig(
        same_process=pytestconfig.option.mypy_same_process,
        test_only_local_stub=pytestconfig.option.mypy_only_local_stub,
        root_directory=pytestconfig.option.mypy_testing_base,
        base_ini_fpath=utils.maybe_abspath(pytestconfig.option.mypy_ini_file),
        base_pyproject_toml_fpath=utils.maybe_abspath(pytestconfig.option.mypy_pyproject_toml_file),
        extension_hook=pytestconfig.option.mypy_extension_hook,
        incremental_cache_dir=os.path.join(pytestconfig.option.mypy_testing_base, ".mypy_cache"),
        mypy_executable=mypy_executable,
        pytest_rootdir=getattr(pytestconfig, "rootdir", None),
        strategy=Strategy(pytestconfig.option.mypy_cache_strategy),
    )


@pytest.fixture()
def mypy_plugins_scenario(
    mypy_plugins_config: MypyPluginsConfig, request: pytest.FixtureRequest
) -> Iterator[MypyPluginsScenario]:
    with mypy_plugins_config.scenario() as scenario:
        request.node.user_properties.append(("mypy_plugins_runs", scenario.runs))
        yield scenario


def pytest_collect_file(file_path: pathlib.Path, parent: Node) -> Optional[pytest.Collector]:
    if file_path.suffix in {".yaml", ".yml"} and file_path.name.startswith(("test-", "test_")):
        return YamlTestFile.from_parent(parent, path=file_path)
    return None


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" and report.outcome == "failed":
        for name, val in report.user_properties:
            if name == "mypy_plugins_runs" and isinstance(val, list):
                report.sections.append(
                    (name, "Ran mypy the following times:\n" + "\n".join(f" - {run}" for run in val)),
                )


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("mypy-tests")
    group.addoption(
        "--mypy-testing-base", type=str, default=tempfile.gettempdir(), help="Base directory for tests to use"
    )
    group.addoption(
        "--mypy-pyproject-toml-file",
        type=str,
        help="Which `pyproject.toml` file to use as a default config for tests. Incompatible with `--mypy-ini-file`",
    )
    group.addoption(
        "--mypy-ini-file",
        type=str,
        help="Which `.ini` file to use as a default config for tests. Incompatible with `--mypy-pyproject-toml-file`",
    )
    group.addoption(
        "--mypy-same-process",
        action="store_true",
        help="Run in the same process. Useful for debugging, will create problems with import cache",
    )
    group.addoption(
        "--mypy-extension-hook",
        type=str,
        help="Fully qualified path to the extension hook function, in case you need custom yaml keys. "
        "Has to be top-level.",
    )
    group.addoption(
        "--mypy-only-local-stub",
        action="store_true",
        help="mypy will ignore errors from site-packages",
    )
    group.addoption(
        "--mypy-closed-schema",
        action="store_true",
        help="Use closed schema to validate YAML test cases, which won't allow any extra keys (does not work well with `--mypy-extension-hook`)",
    )
    group.addoption(
        "--mypy-cache-strategy",
        choices=[strat.name for strat in Strategy],
        help=textwrap.dedent(Strategy.__doc__ or ""),
        default=Strategy.SHARED_INCREMENTAL.value,
    )
