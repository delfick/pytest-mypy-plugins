import contextlib
import dataclasses
import enum
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
    runtime_checkable,
)

from mypy import build
from mypy.fscache import FileSystemCache
from mypy.main import process_options

from . import configs, utils
from .utils import (
    File,
    FollowupFile,
    OutputMatcher,
    TypecheckAssertionError,
    assert_expected_matched_actual,
)


class ReturnCodes:
    SUCCESS = 0
    FAIL = 1
    FATAL_ERROR = 2


def _run_mypy_typechecking(cmd_options: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
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


@dataclasses.dataclass
class Followup:
    main: Optional[str] = None
    description: str = ""
    skip: Union[bool, str] = False
    files: List[FollowupFile] = dataclasses.field(default_factory=list)
    out: Optional[str] = None
    expect_fail: Optional[bool] = None
    additional_properties: Mapping[str, object] = dataclasses.field(default_factory=dict)


class ItemForHook(Protocol):
    """
    The guaranteed available options for a hook
    """

    start: List[str]
    expect_fail: bool
    disable_cache: bool
    additional_mypy_config: str

    @property
    def cache_strategy(self) -> "Strategy":
        pass

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
    def followups(self) -> MutableSequence[Followup]:
        pass

    @property
    def environment_variables(self) -> MutableMapping[str, Any]:
        pass

    @property
    def parsed_test_data(self) -> Mapping[str, Any]:
        pass


@runtime_checkable
class ExtensionHook(Protocol):
    def __call__(self, item: ItemForHook) -> None: ...


class Strategy(enum.Enum):
    """
    The strategy used by the plugin

    SHARED_INCREMENTAL (default)
      - mypy only run once each time. disable_cache doesn't change incremental setting
      - not setting disable_cache uses a shared cache where files are deleted from it
      - after each time mypy is run

    NO_INCREMENTAL
      - mypy is run one for each run with --no-incremental. The disable-cache option
      - does nothing in this strategy

    NON_SHARED_INCREMENTAL
      - mypy is run twice for each run with --incremental.
      - First with an empty cache relative to the temporary directory
      - and again after that cache is made.
      - The disable-cache option prevents the second run in this strategy

    DAEMON
      - A new dmypy is started and run twice for each run
      - The disable-cache option prevents the second run in this strategy
    """

    SHARED_INCREMENTAL = "SHARED_INCREMENTAL"
    NO_INCREMENTAL = "NO_INCREMENTAL"
    NON_SHARED_INCREMENTAL = "NON_SHARED_INCREMENTAL"
    DAEMON = "DAEMON"


@dataclasses.dataclass(frozen=True)
class MypyPluginsConfig:
    """
    Data class representing the mypy plugins specific options from mypy config
    """

    same_process: bool
    strategy: Strategy
    test_only_local_stub: bool
    root_directory: str
    base_ini_fpath: Optional[str]
    base_pyproject_toml_fpath: Optional[str]
    extension_hook: Optional[ExtensionHook]
    scenario_hooks: "ScenarioHooks"
    incremental_cache_dir: str
    mypy_executable: str
    dmypy_executable: Optional[str]
    pytest_rootdir: Optional[Path]

    def __post_init__(self) -> None:
        # You cannot use both `.ini` and `pyproject.toml` files at the same time:
        if self.base_ini_fpath and self.base_pyproject_toml_fpath:
            raise ValueError("Cannot specify both `--mypy-ini-file` and `--mypy-pyproject-toml-file`")

        if self.same_process and self.strategy is Strategy.DAEMON:
            raise ValueError("same-process not implemented for daemon strategy")

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
        scenario = MypyPluginsScenario(
            execution_path=execution_path, mypy_plugins_config=self, scenario_hooks=self.scenario_hooks
        )
        try:
            with utils.cd(execution_path):
                yield scenario
        finally:
            if self.strategy is Strategy.DAEMON and self.dmypy_executable is not None:
                try:
                    subprocess.run(
                        [self.dmypy_executable, "status"], cwd=str(execution_path), capture_output=True, check=True
                    )
                except subprocess.CalledProcessError:
                    pass
                else:
                    subprocess.run(
                        [self.dmypy_executable, "stop"], cwd=str(execution_path), capture_output=True, check=True
                    )
            temp_dir.cleanup()

        assert not os.path.exists(temp_dir.name)

    def execute_extension_hook(self, node: ItemForHook) -> None:
        if self.extension_hook is not None:
            self.extension_hook(node)

    def execute_static_check(
        self,
        *,
        execute_from: Path,
        start: List[str],
        environment_variables: Mapping[str, str],
        disable_cache: bool,
        additional_mypy_config: str,
        output_checker: "OutputChecker",
        run_log: MutableSequence[str],
        config_file: Path,
    ) -> None:
        mypy_cmd_options = [
            "--show-traceback",
            "--no-error-summary",
            "--no-pretty",
            "--hide-error-context",
        ]

        if not self.test_only_local_stub:
            mypy_cmd_options.append("--no-silence-site-packages")

        if config_file:
            mypy_cmd_options.append(f"--config-file={config_file}")

        mypy_short = "mypy"

        if self.strategy is Strategy.SHARED_INCREMENTAL:
            if not disable_cache:
                mypy_cmd_options.extend(["--cache-dir", self.incremental_cache_dir])
        elif self.strategy is Strategy.NO_INCREMENTAL:
            mypy_cmd_options.append("--no-incremental")
        elif self.strategy is Strategy.NON_SHARED_INCREMENTAL:
            mypy_cmd_options.append("--incremental")
        elif self.strategy is Strategy.DAEMON:
            assert self.dmypy_executable is not None
            mypy_short = "dmypy run --"

        mypy_cmd_options.extend(start)

        mypy_executor = MypyExecutor(
            same_process=self.same_process,
            execute_from=execute_from,
            rootdir=self.pytest_rootdir,
            environment_variables=dict(environment_variables),
            mypy_executable=self.mypy_executable,
            dmypy_executable=self.dmypy_executable,
        )

        cache_existed = (execute_from / ".mypy_cache").exists()
        if self.strategy is Strategy.DAEMON:
            cache_existed = (execute_from / ".dmypy.json").exists()
        try:
            run_log.append(f"  % {mypy_short} {' '.join(mypy_cmd_options)}")
            returncode, (stdout, stderr) = mypy_executor.execute(mypy_cmd_options)
            run_log.append(f"  | returncode: {returncode}")
            run_log.extend([f"  | stdout: {line}" for line in stdout.split("\n")])
            run_log.extend([f"  | stderr: {line}" for line in stderr.split("\n")])
            output_checker.check(returncode, stdout, stderr)

            if self.strategy in (Strategy.DAEMON, Strategy.NON_SHARED_INCREMENTAL) and not cache_existed:
                run_log.append("  % ran again")
                returncode, (stdout, stderr) = mypy_executor.execute(mypy_cmd_options)
                run_log.append(f"  | returncode: {returncode}")
                run_log.extend([f"  | stdout: {line}" for line in stdout.split("\n")])
                run_log.extend([f"  | stderr: {line}" for line in stderr.split("\n")])
                output_checker.check(returncode, stdout, stderr)
        finally:
            if self.strategy is Strategy.SHARED_INCREMENTAL and not disable_cache:
                for root, dirs, files in os.walk(execute_from):
                    for name in files:
                        path = (Path(root) / name).relative_to(execute_from)
                        self.remove_cache_files(path.with_suffix(""))

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

    def prepare_config_file(self, execution_path: Path, additional_mypy_config: str) -> Path:
        # Merge (`self.base_ini_fpath` or `base_pyproject_toml_fpath`)
        # and `additional_mypy_config`
        # into one file and copy to the typechecking folder:
        if self.base_pyproject_toml_fpath:
            path = configs.join_toml_configs(self.base_pyproject_toml_fpath, additional_mypy_config, execution_path)
        elif self.base_ini_fpath or additional_mypy_config:
            # We might have `self.base_ini_fpath` set as well.
            # Or this might be a legacy case: only `mypy_config:` is set in the `yaml` test case.
            # This means that no real file is provided.
            path = configs.join_ini_configs(self.base_ini_fpath, additional_mypy_config, execution_path)
        else:
            # assume additional is an ini
            path = configs.join_ini_configs(self.base_ini_fpath, additional_mypy_config, execution_path)

        if path is None:
            location = execution_path / "mypy.ini"
        else:
            location = Path(path)

        if not location.exists():
            location.write_text("[mypy]")

        return location


class MypyExecutor:
    def __init__(
        self,
        same_process: bool,
        rootdir: Optional[Path],
        execute_from: Path,
        environment_variables: MutableMapping[str, Any],
        mypy_executable: str,
        dmypy_executable: Optional[str],
    ) -> None:
        self.rootdir = rootdir
        self.same_process = same_process
        self.execute_from = execute_from
        self.mypy_executable = mypy_executable
        self.dmypy_executable = dmypy_executable
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

        cmd: List[str]
        if self.dmypy_executable is not None:
            cmd = [self.dmypy_executable, "run", "--", *mypy_cmd_options]
        else:
            cmd = [self.mypy_executable, *mypy_cmd_options]

        completed = subprocess.run(
            cmd,
            capture_output=True,
            cwd=self.execute_from,
            env=self.environment_variables,
        )
        captured_stdout = completed.stdout.decode()
        captured_stderr = completed.stderr.decode()
        if self.dmypy_executable is not None:
            captured_stdout = captured_stdout.replace("Daemon started\n", "")
        return completed.returncode, (captured_stdout, captured_stderr)

    def _typecheck_in_same_process(self, mypy_cmd_options: Sequence[Any]) -> Tuple[int, Tuple[str, str]]:
        return_code = -1
        with utils.temp_environ(), utils.temp_path(), utils.temp_sys_modules():
            # add custom environment variables
            for key, val in self.environment_variables.items():
                os.environ[key] = val

            # add current directory to path
            if str(self.execute_from) not in sys.path:
                sys.path.insert(0, str(self.execute_from))

            stdout = io.StringIO()
            stderr = io.StringIO()

            with stdout, stderr:
                return_code = _run_mypy_typechecking(mypy_cmd_options, stdout=stdout, stderr=stderr)
                stdout_value = stdout.getvalue()
                stderr_value = stderr.getvalue()

            return return_code, (stdout_value, stderr_value)

    def _collect_python_path(self, rootdir: Optional[Path]) -> None:
        python_path_parts = []

        existing_python_path = os.environ.get("PYTHONPATH")
        if existing_python_path:
            python_path_parts.append(existing_python_path)
        python_path_parts.append(str(self.execute_from))
        python_path_key = self.environment_variables.get("PYTHONPATH")
        if python_path_key:
            python_path_parts.append(self._maybe_to_abspath(python_path_key, rootdir))
            python_path_parts.append(python_path_key)

        self.environment_variables["PYTHONPATH"] = ":".join(python_path_parts)

    def _collect_mypy_path(self, rootdir: Optional[Path]) -> None:
        mypy_path_parts = []

        existing_mypy_path = os.environ.get("MYPYPATH")
        if existing_mypy_path:
            mypy_path_parts.append(existing_mypy_path)
        mypy_path_key = self.environment_variables.get("MYPYPATH")
        if mypy_path_key:
            mypy_path_parts.append(self._maybe_to_abspath(mypy_path_key, rootdir))
            mypy_path_parts.append(mypy_path_key)
        if rootdir:
            mypy_path_parts.append(str(rootdir))

        self.environment_variables["MYPYPATH"] = ":".join(mypy_path_parts)

    def _maybe_to_abspath(self, rel_or_abs: str, rootdir: Optional[Path]) -> str:
        rel_or_abs = os.path.expandvars(rel_or_abs)
        if rootdir is None or os.path.isabs(rel_or_abs):
            return rel_or_abs
        return str(rootdir / rel_or_abs)


class OutputChecker:
    def __init__(self, expect_fail: bool, execution_path: Path, expected_output: Sequence[OutputMatcher]) -> None:
        self.expect_fail = expect_fail
        self.execution_path = execution_path
        self.expected_output = expected_output

    def check(self, ret_code: int, stdout: str, stderr: str) -> None:
        mypy_output = stdout + stderr
        if ret_code == ReturnCodes.FATAL_ERROR:
            print(mypy_output, file=sys.stderr)
            raise TypecheckAssertionError(error_message="Critical error occurred", mypy_output=mypy_output)

        output_lines = []
        for line in mypy_output.splitlines():
            if ":" in line:
                line = line.strip().replace(".py:", ":")
            output_lines.append(line)
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
    scenario_hooks: "ScenarioHooks"
    mypy_plugins_config: MypyPluginsConfig

    disable_cache: bool = False
    additional_mypy_config: str = ""
    parsed_test_data: Mapping[str, object] = dataclasses.field(default_factory=dict)

    environment_variables: MutableMapping[str, Any] = dataclasses.field(default_factory=dict)

    runs: MutableSequence[str] = dataclasses.field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.runs = [f"Ran mypy the following times from {self.execution_path}"]

    def run_and_check_mypy(
        self,
        start: List[str],
        *,
        expect_fail: bool,
        expected_output: MutableSequence[OutputMatcher],
        additional_properties: Mapping[str, object],
        OutputCheckerKls: type[OutputChecker] = OutputChecker,
    ) -> None:
        config_file = self.mypy_plugins_config.prepare_config_file(self.execution_path, self.additional_mypy_config)

        hook_result = self.scenario_hooks.before_run_and_check_mypy(
            scenario=self,
            options=ScenarioHooksRunAndCheckOptions(
                start=start,
                expect_fail=expect_fail,
            ),
            config_file=config_file,
            expected_output=expected_output,
            additional_properties=additional_properties,
        )

        output_checker = OutputCheckerKls(
            expect_fail=hook_result.expect_fail,
            execution_path=self.execution_path,
            expected_output=expected_output,
        )

        self.mypy_plugins_config.execute_static_check(
            execute_from=self.execution_path,
            start=hook_result.start,
            environment_variables=self.environment_variables,
            disable_cache=self.disable_cache,
            additional_mypy_config=self.additional_mypy_config,
            output_checker=output_checker,
            run_log=self.runs,
            config_file=config_file,
        )

    def path_for(self, path: str, mkdir: bool = False) -> Path:
        location = self.execution_path / path
        if mkdir:
            location.parent.mkdir(parents=True, exist_ok=True)
        return location

    def make_file(self, file: File) -> None:
        current_directory = Path.cwd()
        fpath = current_directory / file.path
        self.runs.append(f"  > Created {fpath}")
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(file.content)

    def handle_followup_file(self, file: FollowupFile) -> None:
        current_directory = Path.cwd()
        fpath = current_directory / file.path
        if file.content is None:
            self.runs.append(f"  > Deleted {fpath}")
            if fpath.is_dir():
                shutil.rmtree(fpath)
            else:
                fpath.unlink(missing_ok=True)
        else:
            mtime_before: Optional[int]
            if fpath.exists():
                if fpath.read_text() == file.content:
                    return

                mtime_before = int(fpath.stat().st_mtime)
                self.runs.append(f"  > Changed {fpath}")
            else:
                mtime_before = None
                self.runs.append(f"  > Created {fpath}")
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(file.content)

            # Need to make sure that the mtime seconds is a different number
            # Otherwise mypy doesn't think it has changed
            while int(fpath.stat().st_mtime) == mtime_before:
                time.sleep(fpath.stat().st_mtime - mtime_before)
                fpath.write_text(file.content)


@dataclasses.dataclass(frozen=True)
class ScenarioHooksRunAndCheckOptions:
    """
    Options passed in and out of the ``before_run_and_check_mypy`` ScenarioHooks hook
    """

    start: List[str]
    expect_fail: bool


class ScenarioHooks:
    def before_run_and_check_mypy(
        self,
        *,
        scenario: MypyPluginsScenario,
        options: ScenarioHooksRunAndCheckOptions,
        config_file: Path,
        expected_output: MutableSequence[OutputMatcher],
        additional_properties: Mapping[str, object],
    ) -> ScenarioHooksRunAndCheckOptions:
        """
        Used to do any adjustments to the scenario before running mypy

        Must return the a ScenarioHooksRunAndCheckOptions object.

        If it's desirable to return the one provided but with different options, ``dataclasses.replace``
        is a good idea: ``return dataclasses.replace(options, expect_fail=True)``
        """
        return options


class ScenarioHookMaker(Protocol):
    def __call__(self) -> ScenarioHooks: ...


if TYPE_CHECKING:
    # Make sure our hooks acts as a valid scenario hook maker
    _sh: ScenarioHookMaker = ScenarioHooks
