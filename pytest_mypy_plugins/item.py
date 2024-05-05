import contextlib
import dataclasses
import importlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    MutableSequence,
    Optional,
    Protocol,
    Sequence,
    TextIO,
    Tuple,
    Union,
)

import pytest
from _pytest._code import ExceptionInfo
from _pytest._code.code import ReprEntry, ReprFileLocation, TerminalRepr
from _pytest._io import TerminalWriter
from mypy import build
from mypy.fscache import FileSystemCache
from mypy.main import process_options

if TYPE_CHECKING:
    from _pytest._code.code import _TracebackStyle

from . import configs, utils
from .definition import File
from .utils import (
    OutputMatcher,
    TypecheckAssertionError,
    assert_expected_matched_actual,
    fname_to_module,
)


class TraceLastReprEntry(ReprEntry):
    def toterminal(self, tw: TerminalWriter) -> None:
        if not self.reprfileloc:
            return

        self.reprfileloc.toterminal(tw)
        for line in self.lines:
            red = line.startswith("E   ")
            tw.line(line, bold=True, red=red)
        return


def make_files(rootdir: Path, files_to_create: Mapping[str, str]) -> Sequence[str]:
    created_modules = []
    for rel_fpath, file_contents in files_to_create.items():
        fpath = rootdir / rel_fpath
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(file_contents)

        created_module = fname_to_module(fpath, root_path=rootdir)
        if created_module:
            created_modules.append(created_module)
    return created_modules


def replace_fpath_with_module_name(line: str, rootdir: Path) -> str:
    if ":" not in line:
        return line
    out_fpath, res_line = line.split(":", 1)
    line = os.path.relpath(out_fpath, start=rootdir) + ":" + res_line
    return line.strip().replace(".py:", ":")


def maybe_to_abspath(rel_or_abs: str, rootdir: Optional[Path]) -> str:
    rel_or_abs = os.path.expandvars(rel_or_abs)
    if rootdir is None or os.path.isabs(rel_or_abs):
        return rel_or_abs
    return str(rootdir / rel_or_abs)


class ReturnCodes:
    SUCCESS = 0
    FAIL = 1
    FATAL_ERROR = 2


def run_mypy_typechecking(cmd_options: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    fscache = FileSystemCache()
    sources, options = process_options(list(cmd_options), fscache=fscache)

    error_messages = []

    # Different mypy versions have different arity of `flush_errors`: 2 and 3 params
    def flush_errors(*args: Any) -> None:
        new_messages: List[str]
        serious: bool
        *_, new_messages, serious = args
        error_messages.extend(new_messages)
        f = stderr if serious else stdout
        try:
            for msg in new_messages:
                f.write(msg + "\n")
            f.flush()
        except BrokenPipeError:
            sys.exit(ReturnCodes.FATAL_ERROR)

    try:
        build.build(sources, options, flush_errors=flush_errors, fscache=fscache, stdout=stdout, stderr=stderr)

    except SystemExit as sysexit:
        # The code to a SystemExit is optional
        # From python docs, if the code is None then the exit code is 0
        # Otherwise if the code is not an integer the exit code is 1
        code = sysexit.code
        if code is None:
            code = 0
        elif not isinstance(code, int):
            code = 1

        return code
    finally:
        fscache.flush()

    if error_messages:
        return ReturnCodes.FAIL

    return ReturnCodes.SUCCESS


class ItemForHook(Protocol):
    """
    The guaranteed available options for a hook
    """

    expect_fail: bool
    disable_cache: bool
    additional_mypy_config: str

    @property
    def files(self) -> MutableSequence[File]:
        pass

    @property
    def starting_lineno(self) -> int:
        pass

    @property
    def expected_output(self) -> MutableSequence[OutputMatcher]:
        pass

    @property
    def environment_variables(self) -> MutableMapping[str, Any]:
        pass

    @property
    def parsed_test_data(self) -> Mapping[str, Any]:
        pass


@dataclasses.dataclass(frozen=True)
class MypyPluginsConfig:
    """
    Data class representing the mypy plugins specific options from mypy config
    """

    same_process: bool
    test_only_local_stub: bool
    root_directory: str
    base_ini_fpath: Optional[str]
    base_pyproject_toml_fpath: Optional[str]
    extension_hook: Optional[str]
    incremental_cache_dir: str
    mypy_executable: str
    pytest_rootdir: Optional[Path]

    def __post_init__(self) -> None:
        # You cannot use both `.ini` and `pyproject.toml` files at the same time:
        if self.base_ini_fpath and self.base_pyproject_toml_fpath:
            raise ValueError("Cannot specify both `--mypy-ini-file` and `--mypy-pyproject-toml-file`")

    @contextlib.contextmanager
    def scenario(self) -> Iterator["MypyPluginsScenario"]:
        """
        Create an execution path, change working directory to it, yield for the test, and perform cleanup.
        """
        try:
            temp_dir = tempfile.TemporaryDirectory(prefix="pytest-mypy-", dir=self.root_directory)

        except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
            raise TypecheckAssertionError(
                error_message=f"Testing base directory {self.root_directory} must exist and be writable"
            ) from e

        execution_path = Path(temp_dir.name)
        scenario = MypyPluginsScenario(execution_path=execution_path, mypy_plugins_config=self)
        try:
            with utils.cd(execution_path):
                yield scenario
        finally:
            temp_dir.cleanup()
            scenario.cleanup_cache()

        assert not os.path.exists(temp_dir.name)

    def execute_extension_hook(self, node: ItemForHook) -> None:
        if self.extension_hook is None:
            return
        module_name, func_name = self.extension_hook.rsplit(".", maxsplit=1)
        module = importlib.import_module(module_name)
        extension_hook = getattr(module, func_name)
        extension_hook(node)

    def remove_cache_files(self, fpath_no_suffix: Path) -> None:
        cache_file = Path(self.incremental_cache_dir)
        cache_file /= ".".join([str(part) for part in sys.version_info[:2]])
        for part in fpath_no_suffix.parts:
            cache_file /= part

        data_json_file = cache_file.with_suffix(".data.json")
        if data_json_file.exists():
            data_json_file.unlink()
        meta_json_file = cache_file.with_suffix(".meta.json")
        if meta_json_file.exists():
            meta_json_file.unlink()

        for parent_dir in cache_file.parents:
            if (
                parent_dir.exists()
                and len(list(parent_dir.iterdir())) == 0
                and str(self.incremental_cache_dir) in str(parent_dir)
            ):
                parent_dir.rmdir()

    def prepare_config_file(self, execution_path: Path, additional_mypy_config: str) -> Optional[str]:
        # Merge (`self.base_ini_fpath` or `base_pyproject_toml_fpath`)
        # and `additional_mypy_config`
        # into one file and copy to the typechecking folder:
        if self.base_pyproject_toml_fpath:
            return configs.join_toml_configs(self.base_pyproject_toml_fpath, additional_mypy_config, execution_path)
        elif self.base_ini_fpath or additional_mypy_config:
            # We might have `self.base_ini_fpath` set as well.
            # Or this might be a legacy case: only `mypy_config:` is set in the `yaml` test case.
            # This means that no real file is provided.
            return configs.join_ini_configs(self.base_ini_fpath, additional_mypy_config, execution_path)
        return None


class MypyExecutor:
    def __init__(
        self,
        same_process: bool,
        rootdir: Optional[Path],
        execution_path: Path,
        environment_variables: MutableMapping[str, Any],
        mypy_executable: str,
    ) -> None:
        self.rootdir = rootdir
        self.same_process = same_process
        self.execution_path = execution_path
        self.mypy_executable = mypy_executable
        self.environment_variables = environment_variables

    def execute(self, mypy_cmd_options: Sequence[str]) -> Tuple[int, Tuple[str, str]]:
        # Returns (returncode, (stdout, stderr))
        if self.same_process:
            return self._typecheck_in_same_process(mypy_cmd_options)
        else:
            return self._typecheck_in_new_subprocess(mypy_cmd_options)

    def _typecheck_in_new_subprocess(self, mypy_cmd_options: Sequence[Any]) -> Tuple[int, Tuple[str, str]]:
        # add current directory to path
        self._collect_python_path(self.rootdir)
        # adding proper MYPYPATH variable
        self._collect_mypy_path(self.rootdir)

        # Windows requires this to be set, otherwise the interpreter crashes
        if "SYSTEMROOT" in os.environ:
            self.environment_variables["SYSTEMROOT"] = os.environ["SYSTEMROOT"]

        completed = subprocess.run(
            [self.mypy_executable, *mypy_cmd_options],
            capture_output=True,
            cwd=os.getcwd(),
            env=self.environment_variables,
        )
        captured_stdout = completed.stdout.decode()
        captured_stderr = completed.stderr.decode()
        return completed.returncode, (captured_stdout, captured_stderr)

    def _typecheck_in_same_process(self, mypy_cmd_options: Sequence[Any]) -> Tuple[int, Tuple[str, str]]:
        return_code = -1
        with utils.temp_environ(), utils.temp_path(), utils.temp_sys_modules():
            # add custom environment variables
            for key, val in self.environment_variables.items():
                os.environ[key] = val

            # add current directory to path
            sys.path.insert(0, str(self.execution_path))

            stdout = io.StringIO()
            stderr = io.StringIO()

            with stdout, stderr:
                return_code = run_mypy_typechecking(mypy_cmd_options, stdout=stdout, stderr=stderr)
                stdout_value = stdout.getvalue()
                stderr_value = stderr.getvalue()

            return return_code, (stdout_value, stderr_value)

    def _collect_python_path(self, rootdir: Optional[Path]) -> None:
        python_path_parts = []

        existing_python_path = os.environ.get("PYTHONPATH")
        if existing_python_path:
            python_path_parts.append(existing_python_path)
        python_path_parts.append(str(self.execution_path))
        python_path_key = self.environment_variables.get("PYTHONPATH")
        if python_path_key:
            python_path_parts.append(maybe_to_abspath(python_path_key, rootdir))
            python_path_parts.append(python_path_key)

        self.environment_variables["PYTHONPATH"] = ":".join(python_path_parts)

    def _collect_mypy_path(self, rootdir: Optional[Path]) -> None:
        mypy_path_parts = []

        existing_mypy_path = os.environ.get("MYPYPATH")
        if existing_mypy_path:
            mypy_path_parts.append(existing_mypy_path)
        mypy_path_key = self.environment_variables.get("MYPYPATH")
        if mypy_path_key:
            mypy_path_parts.append(maybe_to_abspath(mypy_path_key, rootdir))
            mypy_path_parts.append(mypy_path_key)
        if rootdir:
            mypy_path_parts.append(str(rootdir))

        self.environment_variables["MYPYPATH"] = ":".join(mypy_path_parts)


class OutputChecker:
    def __init__(self, expect_fail: bool, execution_path: Path, expected_output: Sequence[OutputMatcher]) -> None:
        self.expect_fail = expect_fail
        self.execution_path = execution_path
        self.expected_output = expected_output

    def check(self, ret_code: int, stdout: str, stderr: str) -> None:
        mypy_output = stdout + stderr
        if ret_code == ReturnCodes.FATAL_ERROR:
            print(mypy_output, file=sys.stderr)
            raise TypecheckAssertionError(error_message="Critical error occurred")

        output_lines = []
        for line in mypy_output.splitlines():
            output_line = replace_fpath_with_module_name(line, rootdir=self.execution_path)
            output_lines.append(output_line)
        try:
            assert_expected_matched_actual(expected=self.expected_output, actual=output_lines)
        except TypecheckAssertionError as e:
            if not self.expect_fail:
                raise e
        else:
            if self.expect_fail:
                raise TypecheckAssertionError("Expected failure, but test passed")


@dataclasses.dataclass
class MypyPluginsScenario:
    execution_path: Path
    mypy_plugins_config: MypyPluginsConfig

    expect_fail: bool = False
    disable_cache: bool = False
    additional_mypy_config: str = ""

    files: MutableSequence[File] = dataclasses.field(default_factory=list)
    expected_output: MutableSequence[OutputMatcher] = dataclasses.field(default_factory=list)
    environment_variables: MutableMapping[str, Any] = dataclasses.field(default_factory=dict)

    def cleanup_cache(self) -> None:
        if not self.disable_cache:
            for file in self.files:
                path = Path(file.path)
                self.mypy_plugins_config.remove_cache_files(path.with_suffix(""))

    def _prepare_mypy_cmd_options(self, main_file: Path) -> Sequence[str]:
        config_file = self.mypy_plugins_config.prepare_config_file(self.execution_path, self.additional_mypy_config)

        mypy_cmd_options = [
            "--show-traceback",
            "--no-error-summary",
            "--no-pretty",
            "--hide-error-context",
        ]

        if not self.mypy_plugins_config.test_only_local_stub:
            mypy_cmd_options.append("--no-silence-site-packages")

        if not self.disable_cache:
            mypy_cmd_options.extend(["--cache-dir", self.mypy_plugins_config.incremental_cache_dir])

        if config_file:
            mypy_cmd_options.append(f"--config-file={config_file}")

        mypy_cmd_options.append(str(main_file))

        return mypy_cmd_options

    def run_and_check_mypy(self, main_file: str) -> None:
        mypy_executor = MypyExecutor(
            same_process=self.mypy_plugins_config.same_process,
            execution_path=self.execution_path,
            rootdir=self.mypy_plugins_config.pytest_rootdir,
            environment_variables=self.environment_variables,
            mypy_executable=self.mypy_plugins_config.mypy_executable,
        )

        output_checker = OutputChecker(
            expect_fail=self.expect_fail, execution_path=self.execution_path, expected_output=self.expected_output
        )

        mypy_cmd_options = self._prepare_mypy_cmd_options(self.execution_path / main_file)

        returncode, (stdout, stderr) = mypy_executor.execute(mypy_cmd_options)
        output_checker.check(returncode, stdout, stderr)

    def make_file(self, file: File) -> None:
        self.files.append(file)
        current_directory = Path.cwd()
        fpath = current_directory / file.path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(file.content)


class YamlTestItem(pytest.Item):
    def __init__(
        self,
        name: str,
        parent: Optional[pytest.Collector] = None,
        *,
        files: MutableSequence[File],
        starting_lineno: int,
        expected_output: MutableSequence[OutputMatcher],
        environment_variables: MutableMapping[str, Any],
        disable_cache: bool,
        mypy_config: str,
        parsed_test_data: Mapping[str, Any],
        expect_fail: bool,
    ) -> None:
        super().__init__(name, parent)
        self.files = files
        self.environment_variables = environment_variables
        self.disable_cache = disable_cache
        self.expect_fail = expect_fail
        self.expected_output = expected_output
        self.starting_lineno = starting_lineno
        self.additional_mypy_config = mypy_config
        self.parsed_test_data = parsed_test_data

    def runtest(self) -> None:
        node = pytest.Function.from_parent(self, name=self.name, callobj=self._runtest)
        node.setup()
        node.runtest()

    def _runtest(self, mypy_plugins_config: MypyPluginsConfig) -> None:
        # extension point for derived packages
        mypy_plugins_config.execute_extension_hook(self)

        with mypy_plugins_config.scenario() as scenario:
            scenario.disable_cache = self.disable_cache
            scenario.environment_variables = self.environment_variables
            scenario.additional_mypy_config = self.additional_mypy_config
            scenario.expect_fail = self.expect_fail
            scenario.expected_output = self.expected_output

            for file in self.files:
                scenario.make_file(file)

            scenario.run_and_check_mypy("main.py")

    def repr_failure(
        self, excinfo: ExceptionInfo[BaseException], style: Optional["_TracebackStyle"] = None
    ) -> Union[str, TerminalRepr]:
        if isinstance(excinfo.value, SystemExit):
            # We assume that before doing exit() (which raises SystemExit) we've printed
            # enough context about what happened so that a stack trace is not useful.
            # In particular, uncaught exceptions during semantic analysis or type checking
            # call exit() and they already print out a stack trace.
            return excinfo.exconly(tryshort=True)
        elif isinstance(excinfo.value, TypecheckAssertionError):
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
            return super().repr_failure(excinfo, style="native")

    def reportinfo(self) -> Tuple[Union[Path, str], Optional[int], str]:
        return self.path, None, self.name
